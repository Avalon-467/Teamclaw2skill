# =========== Copyright 2023 @ CAMEL-AI.org. All Rights Reserved. ===========
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# =========== Copyright 2023 @ CAMEL-AI.org. All Rights Reserved. ===========
"""Post vector generation via OpenAI-compatible Embedding API."""
from typing import List

import numpy as np
from camel.embeddings import OpenAIEmbedding
from camel.types import EmbeddingModelType


def generate_post_vector(texts: List[str], batch_size: int = 1000) -> np.ndarray:
    """Generate embeddings using OpenAI Embedding API.

    Args:
        texts: List of texts to embed.
        batch_size: Texts per API call.

    Returns:
        np.ndarray of shape (len(texts), embedding_dim).
    """
    openai_embedding = OpenAIEmbedding(
        model_type=EmbeddingModelType.TEXT_EMBEDDING_3_SMALL)

    all_embeddings = []
    for i in range(0, len(texts), batch_size):
        batch_texts = texts[i:i + batch_size]
        cleaned_texts = [
            text.strip() if text and isinstance(text, str) else "empty"
            for text in batch_texts
        ]
        batch_embeddings = openai_embedding.embed_list(objs=cleaned_texts)
        all_embeddings.append(np.array(batch_embeddings))

    return np.concatenate(all_embeddings, axis=0)


