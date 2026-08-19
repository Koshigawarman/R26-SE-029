import subprocess
import time
import os
import socket
import modal

app = modal.App("multi-agent-ollama")

# Connect to the volume containing your uploaded .gguf files
model_volume = modal.Volume.from_name("agent-models-vol", create_if_missing=True)

# Build container image with Ollama pre-installed
ollama_image = (
    modal.Image.debian_slim()
    .apt_install("curl", "zstd", "pciutils")
    .run_commands("curl -fsSL https://ollama.com/install.sh | sh")
)

def _wait_for_port(host: str, port: int, timeout: int = 60) -> bool:
    """Poll until a TCP connection to host:port succeeds or timeout expires."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1):
                return True
        except OSError:
            time.sleep(0.5)
    return False


# Allocate a 24GB VRAM A10G GPU to run all 4 models without memory swapping
@app.function(
    image=ollama_image,
    gpu="A10G",
    volumes={"/models": model_volume},
    timeout=1800,  # 30 min runtime limit per session
    # keep_warm=1   # UNCOMMENT THIS ON DEMO DAY TO PREVENT COLD STARTS
)
@modal.web_server(port=11434, startup_timeout=300)
def serve_ollama():
    # Set Ollama models directory to the persistent volume
    os.environ["OLLAMA_MODELS"] = "/models/ollama_data"

    # Explicitly bind Ollama to all IPv4 interfaces on port 11434
    # (Modal's startup health check connects to 127.0.0.1:11434)
    os.environ["OLLAMA_HOST"] = "0.0.0.0:11434"
    os.environ["OLLAMA_ORIGINS"] = "*"

    models = {
        "qwen-critic": "critic_agent.gguf"
    }

    # Fast path: Check if all models are already created using marker files
    needs_setup = any(
        not os.path.exists(f"/models/.created_{name}") for name in models
    )

    if needs_setup:
        # Use a side-car daemon on port 11435 to create the model
        # so port 11434 stays free for Modal's startup check
        env = os.environ.copy()
        env["OLLAMA_HOST"] = "127.0.0.1:11435"

        print("Starting temporary Ollama daemon for model setup...")
        setup_process = subprocess.Popen(["ollama", "serve"], env=env)

        if not _wait_for_port("127.0.0.1", 11435, timeout=30):
            print("❌ Temporary daemon did not start in time. Aborting setup.")
            setup_process.terminate()
        else:
            for model_name, file_name in models.items():
                if os.path.exists(f"/models/.created_{model_name}"):
                    continue

                file_path = f"/models/{file_name}"
                if os.path.exists(file_path):
                    res = subprocess.run(
                        ["ollama", "list"], env=env,
                        capture_output=True, text=True
                    )
                    if model_name not in res.stdout:
                        print(f"Registering model: {model_name}...")
                        with open("Modelfile", "w") as f:
                            f.write(f"FROM {file_path}")
                        subprocess.run(
                            ["ollama", "create", model_name, "-f", "Modelfile"],
                            env=env
                        )
                    else:
                        print(f"Model {model_name} already exists in persistent storage.")

                    # Write marker so we skip this on future cold starts
                    with open(f"/models/.created_{model_name}", "w") as f:
                        f.write("ready")
                else:
                    print(
                        f"❌ ERROR: /models/{file_name} not found in the volume! "
                        "Upload it with: modal volume put agent-models-vol critic_agent.gguf /"
                    )

            setup_process.terminate()
            setup_process.wait()

    # Start the real public Ollama daemon
    print("Starting public Ollama daemon on 0.0.0.0:11434...")
    process = subprocess.Popen(["ollama", "serve"])

    # Poll until Ollama is actually accepting TCP connections on 127.0.0.1:11434
    # This is the same check Modal's startup detector uses.
    print("Waiting for Ollama to accept connections on 127.0.0.1:11434...")
    ready = _wait_for_port("127.0.0.1", 11434, timeout=60)
    if ready:
        print("✅ Ollama is UP and accepting connections! Modal proxy is now active.")
    else:
        print("⚠️  Ollama did not bind to 127.0.0.1:11434 within 60s — Modal may not route traffic correctly.")

    # Block to keep the container alive
    process.wait()