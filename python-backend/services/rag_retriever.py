import json
import faiss
import numpy as np
from sentence_transformers import SentenceTransformer


class RAGRetriever:

    def __init__(self):

        # Load embedding model
        self.model = SentenceTransformer('all-MiniLM-L6-v2')

        # Load knowledge base
        with open('datasets/knowledge_base.json', 'r') as f:
            self.data = json.load(f)

        # Extract texts
        self.texts = [item["content"] for item in self.data]

        # Convert to embeddings
        self.embeddings = self.model.encode(self.texts)

        # Create FAISS index
        dimension = len(self.embeddings[0])

        self.index = faiss.IndexFlatL2(dimension)

        self.index.add(np.array(self.embeddings))

    def retrieve(self, query, top_k=2):

        # Convert query into embedding
        query_embedding = self.model.encode([query])

        # Search similar examples
        distances, indices = self.index.search(
            np.array(query_embedding),
            top_k
        )

        results = []

        for i in indices[0]:
            results.append(self.texts[i])

        return results