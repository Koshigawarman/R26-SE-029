import json
import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import chromadb
from chromadb.utils import embedding_functions

from schema import MemoryCase, MemoryMatch, RuntimeErrorInfo, CriticStrategy

logger = logging.getLogger(__name__)


class EpisodicMemory:
    """
    ChromaDB-based Episodic Memory.

    Responsibility:
    - Store successful error-to-fix strategy cases in a local Vector Store.
    - Retrieve similar past cases for the current runtime error via semantic search.
    - Provide memory context to the Critic Agent.
    """

    def __init__(self, memory_dir: str = "memory/chroma_db"):
        self.memory_dir = Path(memory_dir)
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        
        # Initialize ChromaDB persistent client
        self.client = chromadb.PersistentClient(path=str(self.memory_dir))
        
        # We use the default sentence transformer embedding model
        # which creates a dense vector representation of the errors.
        self.embedding_fn = embedding_functions.DefaultEmbeddingFunction()

        # Create or get the collection
        self.collection = self.client.get_or_create_collection(
            name="error_fixes",
            embedding_function=self.embedding_fn,
            metadata={"hnsw:space": "cosine"} # Use cosine similarity
        )

        logger.info(f"Episodic memory initialized at {self.memory_dir}. Collection count: {self.collection.count()}")

    def load_cases(self) -> List[MemoryCase]:
        """
        Retrieves all cases from ChromaDB. Primarily used for seeding/debugging.
        """
        try:
            results = self.collection.get(include=["metadatas"])
            if not results or not results["metadatas"]:
                return []
            
            cases = []
            for meta in results["metadatas"]:
                if "case_json" in meta:
                    cases.append(MemoryCase(**json.loads(meta["case_json"])))
            return cases

        except Exception as e:
            logger.error(f"Failed to load episodic memory cases: {e}")
            return []

    def retrieve_similar(
        self,
        errors: List[RuntimeErrorInfo],
        stderr: str = "",
        top_k: int = 3
    ) -> List[MemoryMatch]:
        """
        Retrieves similar memory cases using semantic vector search in ChromaDB.
        """
        if self.collection.count() == 0:
            return []

        query_text = self._build_query_text(errors, stderr)
        if not query_text.strip():
            return []

        try:
            results = self.collection.query(
                query_texts=[query_text],
                n_results=min(top_k, self.collection.count()),
                include=["metadatas", "distances", "documents"]
            )

            if not results["metadatas"] or not results["metadatas"][0]:
                return []

            matches: List[MemoryMatch] = []
            
            # ChromaDB distances for cosine are 1 - cosine_similarity.
            # So a distance of 0 is a perfect match (score=1.0).
            for i, meta in enumerate(results["metadatas"][0]):
                distance = results["distances"][0][i]
                score = max(0.0, 1.0 - distance) # Convert distance to similarity score
                
                if "case_json" in meta:
                    case = MemoryCase(**json.loads(meta["case_json"]))
                    matches.append(
                        MemoryMatch(
                            case=case,
                            score=round(score, 3),
                            matched_pattern=case.error_pattern
                        )
                    )

            if matches:
                logger.info(f"Episodic memory retrieved {len(matches)} similar case(s) via semantic search.")
            else:
                logger.info("No similar episodic memory case found.")

            return matches

        except Exception as e:
            logger.error(f"Error during semantic retrieval: {e}")
            return []


    def store_success_case(
        self,
        errors: List[RuntimeErrorInfo],
        critic_strategy: CriticStrategy,
        fixed_files: Optional[List[str]] = None
    ) -> None:
        """
        Stores an error-to-success case after the system verifies that the fix worked.
        """
        fixed_files = fixed_files or []
        error_pattern = self._extract_error_pattern(errors)

        new_case = MemoryCase(
            error_pattern=error_pattern,
            root_cause=critic_strategy.root_cause,
            fix_strategy=critic_strategy.fixing_strategy,
            affected_files=fixed_files or critic_strategy.affected_files,
            success=True,
            usage_count=1,
            created_at=datetime.utcnow().isoformat()
        )

        # Before adding, let's query if a very similar fix strategy already exists
        # to prevent duplicate embeddings from overwhelming the space.
        existing = self.collection.query(
            query_texts=[new_case.fix_strategy],
            n_results=1,
            include=["metadatas", "distances"]
        )

        if existing["metadatas"] and existing["metadatas"][0]:
            best_distance = existing["distances"][0][0]
            if best_distance < 0.05: # Extremely similar (cosine distance < 0.05)
                logger.info("Similar memory case already exists. Skipping insertion to prevent duplication.")
                return

        # Prepare document for semantic indexing
        document_text = f"Error: {error_pattern}\nRoot Cause: {new_case.root_cause}\nFix Strategy: {new_case.fix_strategy}"
        
        case_id = str(uuid.uuid4())
        
        try:
            self.collection.add(
                documents=[document_text],
                metadatas=[{"case_json": new_case.model_dump_json()}],
                ids=[case_id]
            )
            logger.info(f"Stored new episodic memory case in ChromaDB: {error_pattern}")
        except Exception as e:
            logger.error(f"Failed to store case in ChromaDB: {e}")


    def seed_from_dataset(self, dataset_path: str = "datasets/error_fix_cases.json") -> int:
        """
        Loads initial error-fix cases from JSON into ChromaDB.
        """
        path = Path(dataset_path)

        if not path.exists():
            logger.warning(f"Dataset file not found: {dataset_path}")
            return 0

        try:
            raw = path.read_text(encoding="utf-8")
            data = json.loads(raw)

            added_count = 0
            
            documents = []
            metadatas = []
            ids = []

            for item in data:
                new_case = MemoryCase(
                    error_pattern=item.get("error_pattern", "Unknown error"),
                    root_cause=item.get("root_cause", "Unknown root cause"),
                    fix_strategy=item.get("fix_strategy", "Apply minimal fix"),
                    affected_files=item.get("affected_files", []),
                    success=True,
                    usage_count=0,
                    created_at=datetime.utcnow().isoformat()
                )

                doc_text = f"Error: {new_case.error_pattern}\nRoot Cause: {new_case.root_cause}\nFix Strategy: {new_case.fix_strategy}"
                
                # Check for duplicates
                existing = self.collection.query(
                    query_texts=[doc_text],
                    n_results=1,
                    include=["distances"]
                )
                
                if not (existing["distances"] and existing["distances"][0] and existing["distances"][0][0] < 0.05):
                    documents.append(doc_text)
                    metadatas.append({"case_json": new_case.model_dump_json()})
                    ids.append(str(uuid.uuid4()))
                    added_count += 1

            if documents:
                self.collection.add(
                    documents=documents,
                    metadatas=metadatas,
                    ids=ids
                )
                logger.info(f"Seeded {added_count} memory case(s) into ChromaDB from dataset")
            
            return added_count

        except Exception as e:
            logger.error(f"Failed to seed episodic memory: {e}")
            return 0

    def _build_query_text(self, errors: List[RuntimeErrorInfo], stderr: str) -> str:
        parts = []

        for err in errors:
            parts.append(err.message)
            parts.append(err.type)
            if err.file:
                parts.append(err.file)
            parts.append(err.stack[:1000])

        if stderr:
            parts.append(stderr[:1000])

        return " ".join(parts)

    def _extract_error_pattern(self, errors: List[RuntimeErrorInfo]) -> str:
        if not errors:
            return "Unknown error"

        first = errors[0]
        message = first.message or ""

        common_patterns = [
            "Cannot find module",
            "MODULE_NOT_FOUND",
            "SyntaxError",
            "ReferenceError",
            "TypeError",
            "EADDRINUSE",
            "Cannot GET",
            "Router.use() requires a middleware function",
            "Cannot read properties of undefined"
        ]

        for pattern in common_patterns:
            if pattern.lower() in message.lower() or pattern.lower() in first.stack.lower():
                return pattern

        return message[:120] if message else first.type