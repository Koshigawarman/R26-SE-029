import json
import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

import chromadb
from chromadb.utils import embedding_functions

logger = logging.getLogger(__name__)


class PlanMemory:
    """
    ChromaDB-based Plan Memory.

    Responsibility:
    - Store successful/approved project plans.
    - Retrieve similar past approved plans via semantic search on the user prompt.
    """

    def __init__(self, memory_dir: str = "memory/chroma_db"):
        self.memory_dir = Path(memory_dir)
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        
        self.client = chromadb.PersistentClient(path=str(self.memory_dir))
        self.embedding_fn = embedding_functions.DefaultEmbeddingFunction()

        self.collection = self.client.get_or_create_collection(
            name="approved_plans",
            embedding_function=self.embedding_fn,
            metadata={"hnsw:space": "cosine"}
        )

        logger.info(f"Plan memory initialized at {self.memory_dir}. Collection count: {self.collection.count()}")

    def store_approved_plan(self, user_prompt: str, plan_json: str) -> None:
        if not user_prompt.strip() or not plan_json.strip():
            return
            
        case_id = str(uuid.uuid4())
        
        existing = self.collection.query(
            query_texts=[user_prompt],
            n_results=1,
            include=["distances"]
        )
        if existing["distances"] and existing["distances"][0]:
            best_distance = existing["distances"][0][0]
            if best_distance < 0.05:
                logger.info("Similar plan already exists in memory. Skipping insertion.")
                return

        try:
            self.collection.add(
                documents=[user_prompt],
                metadatas=[{
                    "plan_json": plan_json,
                    "created_at": datetime.utcnow().isoformat()
                }],
                ids=[case_id]
            )
            logger.info("Stored new approved plan in ChromaDB.")
        except Exception as e:
            logger.error(f"Failed to store plan in ChromaDB: {e}")

    def retrieve_similar_plan(self, user_prompt: str, top_k: int = 1) -> Optional[str]:
        if self.collection.count() == 0:
            return None

        if not user_prompt.strip():
            return None

        try:
            results = self.collection.query(
                query_texts=[user_prompt],
                n_results=top_k,
                include=["metadatas", "distances"]
            )

            if not results["metadatas"] or not results["metadatas"][0]:
                return None

            best_distance = results["distances"][0][0]
            score = max(0.0, 1.0 - best_distance)
            
            # Require at least some similarity
            if score < 0.5:
                return None
                
            meta = results["metadatas"][0][0]
            if "plan_json" in meta:
                logger.info(f"Retrieved similar plan (score: {score:.3f})")
                return meta["plan_json"]
                
            return None

        except Exception as e:
            logger.error(f"Error retrieving similar plan: {e}")
            return None

    def seed_from_dataset(self, dataset_path: str = "datasets/approved_plans.json") -> int:
        """
        Loads initial approved plan cases from JSON into ChromaDB.
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
                user_prompt = item.get("user_prompt", "")
                plan_json = item.get("plan_json", "")
                
                if not user_prompt or not plan_json:
                    continue

                # Check for duplicates
                existing = self.collection.query(
                    query_texts=[user_prompt],
                    n_results=1,
                    include=["distances"]
                )
                
                if not (existing["distances"] and existing["distances"][0] and existing["distances"][0][0] < 0.05):
                    documents.append(user_prompt)
                    metadatas.append({
                        "plan_json": plan_json,
                        "created_at": datetime.utcnow().isoformat()
                    })
                    ids.append(str(uuid.uuid4()))
                    added_count += 1

            if documents:
                self.collection.add(
                    documents=documents,
                    metadatas=metadatas,
                    ids=ids
                )
                logger.info(f"Seeded {added_count} approved plan(s) into ChromaDB from dataset")
            
            return added_count

        except Exception as e:
            logger.error(f"Failed to seed plan memory: {e}")
            return 0
