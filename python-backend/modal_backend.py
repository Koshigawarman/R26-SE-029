import modal
import os

app = modal.App("multi-agent-gpu-backend")

# Connect to the volume containing your uploaded .gguf files
vol = modal.Volume.from_name("agent-models-vol", create_if_missing=True)

# Use an official NVIDIA CUDA 12 image so libcudart.so.12 is available
image = (
    modal.Image.from_registry("nvidia/cuda:12.1.1-runtime-ubuntu22.04", add_python="3.11")
    .apt_install("libgomp1")
    .pip_install("fastapi", "uvicorn", "sse-starlette")
    .pip_install(
        "llama-cpp-python[server]==0.2.89",
        extra_options="--extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cu121"
    )
)

@app.function(
    image=image, 
    gpu="A10G",           # Use a fast 24GB VRAM GPU
    volumes={"/models": vol},
    timeout=1800          # 30 min runtime limit per session
)
@modal.concurrent(max_inputs=10) # Allow multiple agents to hit the API at once
@modal.asgi_app()
def serve():
    from llama_cpp.server.app import create_app
    from llama_cpp.server.settings import ServerSettings, ModelSettings
    
    # We will standardize the filename to 'agent_model.gguf'
    model_path = "/models/critic_model.gguf"
    
    if not os.path.exists(model_path):
        error_msg = (
            f"❌ ERROR: Model not found at {model_path}!\n\n"
            "Please upload your GGUF file to Modal by running this command in your terminal:\n"
            "modal volume put agent-models-vol <YOUR_LOCAL_FILE.gguf> /agent_model.gguf\n"
        )
        print(error_msg)
        raise FileNotFoundError(error_msg)
        
    print(f"Loading model from {model_path} into GPU memory...")
    
    server_settings = ServerSettings(
        host="0.0.0.0",
        port=8000
    )
    
    model_settings = [ModelSettings(
        model=model_path,
        model_alias="critic_model", # This is the model name you will use in .env
        n_gpu_layers=-1,           # Offload all layers to GPU for max speed
        n_ctx=8192,                # 8k context window (good for coding)
        chat_format="chatml"       # Adjust this if your model uses a different prompt format (e.g. llama3)
    )]
    
    # Create the FastAPI app that perfectly mocks the OpenAI API
    app = create_app(
        server_settings=server_settings,
        model_settings=model_settings
    )
    
    print("✅ Model loaded successfully! OpenAI-compatible API is ready.")
    return app
