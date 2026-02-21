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
"""Recommendation system for the OASIS social platform."""
import heapq
import logging
import random
import time
from ast import literal_eval
from datetime import datetime
from math import log
from typing import Any, Dict, List

import numpy as np
from sklearn.metrics.pairwise import cosine_similarity as sklearn_cosine_similarity

from .process_recsys_posts import generate_post_vector
from .typing import ActionType, RecsysType

rec_log = logging.getLogger(name='social.rec')
rec_log.setLevel('DEBUG')

# ── Global caches ──
# All historical tweets and the most recent tweet of each user
user_previous_post_all: Dict[int, list] = {}
user_previous_post: Dict[int, str] = {}
user_profiles: List[str] = []
# {post_id: content}
t_items: Dict[int, str] = {}
# {uid: follower_count}
u_items: Dict[int, int] = {}
# Recency scores for each tweet
date_score: List[float] = []


def reset_globals():
    """Reset global variables between runs."""
    global user_previous_post_all, user_previous_post
    global user_profiles, t_items, u_items
    global date_score
    user_previous_post_all = {}
    user_previous_post = {}
    user_profiles = []
    t_items = {}
    u_items = {}
    date_score = []


# ─────────────────────────────────────────────────────────────────────
#  Simple recommendation strategies (no embedding needed)
# ─────────────────────────────────────────────────────────────────────

def rec_sys_random(post_table: List[Dict[str, Any]], rec_matrix: List[List],
                   max_rec_post_len: int) -> List[List]:
    """Randomly recommend posts to users."""
    post_ids = [post['post_id'] for post in post_table]
    if len(post_ids) <= max_rec_post_len:
        return [post_ids] * len(rec_matrix)
    return [random.sample(post_ids, max_rec_post_len) for _ in range(len(rec_matrix))]


def calculate_hot_score(num_likes: int, num_dislikes: int,
                        created_at: datetime) -> int:
    """Compute Reddit-style hot score for a post."""
    s = num_likes - num_dislikes
    order = log(max(abs(s), 1), 10)
    sign = 1 if s > 0 else -1 if s < 0 else 0
    epoch = datetime(1970, 1, 1)
    td = created_at - epoch
    epoch_seconds_result = td.days * 86400 + td.seconds + (
        float(td.microseconds) / 1e6)
    seconds = epoch_seconds_result - 1134028003
    return round(sign * order + seconds / 45000, 7)


def get_recommendations(user_index, cosine_similarities, items, score,
                        top_n=100):
    similarities = np.array(cosine_similarities[user_index])
    similarities = similarities * score
    top_item_indices = similarities.argsort()[::-1][:top_n]
    recommended_items = [(list(items.keys())[i], similarities[i])
                         for i in top_item_indices]
    return recommended_items


def rec_sys_reddit(post_table: List[Dict[str, Any]], rec_matrix: List[List],
                   max_rec_post_len: int) -> List[List]:
    """Recommend posts based on Reddit-like hot score."""
    post_ids = [post['post_id'] for post in post_table]

    if len(post_ids) <= max_rec_post_len:
        return [post_ids] * len(rec_matrix)

    all_hot_score = []
    for post in post_table:
        try:
            created_at_dt = datetime.strptime(post['created_at'],
                                              "%Y-%m-%d %H:%M:%S.%f")
        except Exception:
            created_at_dt = datetime.strptime(post['created_at'],
                                              "%Y-%m-%d %H:%M:%S")
        hot_score = calculate_hot_score(post['num_likes'],
                                        post['num_dislikes'], created_at_dt)
        all_hot_score.append((hot_score, post['post_id']))
    top_posts = heapq.nlargest(max_rec_post_len, all_hot_score,
                               key=lambda x: x[0])
    top_post_ids = [post_id for _, post_id in top_posts]
    return [top_post_ids] * len(rec_matrix)


# ─────────────────────────────────────────────────────────────────────
#  Embedding-based personalized recommendation (OpenAI API)
# ─────────────────────────────────────────────────────────────────────

def _np_cosine_similarity(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Cosine similarity between two 2-D matrices → (len(a), len(b))."""
    a_norm = a / (np.linalg.norm(a, axis=1, keepdims=True) + 1e-10)
    b_norm = b / (np.linalg.norm(b, axis=1, keepdims=True) + 1e-10)
    return a_norm @ b_norm.T


def rec_sys_personalized(user_table: List[Dict[str, Any]],
                         post_table: List[Dict[str, Any]],
                         trace_table: List[Dict[str, Any]],
                         rec_matrix: List[List],
                         max_rec_post_len: int) -> List[List]:
    """Personalized recommendation using OpenAI Embedding API + numpy."""
    post_ids = [post['post_id'] for post in post_table]
    print(f'Running personalized recommendation for {len(user_table)} users...')
    start_time = time.time()
    new_rec_matrix = []

    if len(post_ids) <= max_rec_post_len:
        new_rec_matrix = [post_ids] * len(rec_matrix)
    else:
        user_bios = [
            user['bio'] if 'bio' in user and user['bio'] is not None else ''
            for user in user_table
        ]
        post_contents = [post['content'] for post in post_table]

        # Get embeddings via API
        user_embeddings = generate_post_vector(user_bios, batch_size=1000)
        post_embeddings = generate_post_vector(post_contents, batch_size=1000)

        # Cosine similarity  (users x posts)
        similarities = _np_cosine_similarity(user_embeddings, post_embeddings)

        for user_index, user in enumerate(user_table):
            filtered_post_indices = [
                i for i, post in enumerate(post_table)
                if post['user_id'] != user['user_id']
            ]
            user_sim = similarities[user_index, filtered_post_indices]
            filtered_post_ids = [
                post_table[i]['post_id'] for i in filtered_post_indices
            ]
            k = min(max_rec_post_len, len(filtered_post_ids))
            top_indices = np.argsort(user_sim)[::-1][:k]
            top_post_ids = [filtered_post_ids[i] for i in top_indices]
            new_rec_matrix.append(top_post_ids)

    end_time = time.time()
    print(f'Personalized recommendation time: {end_time - start_time:.6f}s')
    return new_rec_matrix


# ─────────────────────────────────────────────────────────────────────
#  Helper functions for trace-based recommendation
# ─────────────────────────────────────────────────────────────────────

def get_like_post_id(user_id, action, trace_table):
    """Get post IDs that a user has liked/unliked."""
    trace_post_ids = [
        literal_eval(trace['info'])["post_id"] for trace in trace_table
        if (trace['user_id'] == user_id and trace['action'] == action)
    ]
    if len(trace_post_ids) < 5 and len(trace_post_ids) > 0:
        trace_post_ids += [trace_post_ids[-1]] * (5 - len(trace_post_ids))
    elif len(trace_post_ids) > 5:
        trace_post_ids = trace_post_ids[-5:]
    else:
        trace_post_ids = [0]
    return trace_post_ids


def calculate_like_similarity(liked_vectors, target_vectors):
    """Average cosine similarity between liked posts and target posts."""
    liked_norms = np.linalg.norm(liked_vectors, axis=1)
    target_norms = np.linalg.norm(target_vectors, axis=1)
    dot_products = np.dot(target_vectors, liked_vectors.T)
    cosine_similarities = dot_products / (np.outer(target_norms, liked_norms) + 1e-10)
    return np.mean(cosine_similarities, axis=1)


def coarse_filtering(input_list, scale):
    """Coarse filtering posts and return selected elements with indices."""
    if len(input_list) <= scale:
        return (input_list, list(range(len(input_list))))
    sampled_indices = random.sample(range(len(input_list)), scale)
    sampled_elements = [input_list[idx] for idx in sampled_indices]
    return (sampled_elements, sampled_indices)


# ─────────────────────────────────────────────────────────────────────
#  TWH-style personalized recommendation (API version)
# ─────────────────────────────────────────────────────────────────────

def rec_sys_personalized_twh(
        user_table: List[Dict[str, Any]],
        post_table: List[Dict[str, Any]],
        latest_post_count: int,
        trace_table: List[Dict[str, Any]],
        rec_matrix: List[List],
        max_rec_post_len: int,
        current_time: int,
        recall_only: bool = False,
        enable_like_score: bool = False,
        use_openai_embedding: bool = True) -> List[List]:
    """Personalized recommendation (twhin-style) using OpenAI API embeddings."""
    global date_score, t_items, u_items, user_previous_post
    global user_previous_post_all, user_profiles

    if (not u_items) or len(u_items) != len(user_table):
        u_items = {user['user_id']: user["num_followers"] for user in user_table}
    if not user_previous_post_all or len(user_previous_post_all) != len(user_table):
        user_previous_post_all = {index: [] for index in range(len(user_table))}
        user_previous_post = {index: "" for index in range(len(user_table))}
    if not user_profiles or len(user_profiles) != len(user_table):
        for user in user_table:
            if user['bio'] is None:
                user_profiles.append('This user does not have profile')
            else:
                user_profiles.append(user['bio'])

    if len(t_items) < len(post_table):
        for post in post_table[-latest_post_count:]:
            t_items[post['post_id']] = post['content']
            user_previous_post_all[post['user_id']].append(post['content'])
            user_previous_post[post['user_id']] = post['content']
            date_score.append(
                np.log((271.8 - (current_time - int(post['created_at']))) / 100))

    date_score_np = np.array(date_score)

    if enable_like_score:
        like_post_ids_all = []
        for user in user_table:
            user_id = user['agent_id']
            like_post_ids = get_like_post_id(user_id,
                                             ActionType.LIKE_POST.value,
                                             trace_table)
            like_post_ids_all.append(like_post_ids)

    scores = date_score_np
    new_rec_matrix = []

    if len(post_table) <= max_rec_post_len:
        tids = [t['post_id'] for t in post_table]
        new_rec_matrix = [tids] * len(rec_matrix)
    else:
        # Update user profiles with recent posts
        for post_user_index in user_previous_post:
            try:
                update_profile = (
                    f" # Recent post:{user_previous_post[post_user_index]}")
                if user_previous_post[post_user_index] != "":
                    if "# Recent post:" not in user_profiles[post_user_index]:
                        user_profiles[post_user_index] += update_profile
                    elif update_profile not in user_profiles[post_user_index]:
                        user_profiles[post_user_index] = user_profiles[
                            post_user_index].split(
                                "# Recent post:")[0] + update_profile
            except Exception:
                print("update previous post failed")

        # Coarse filtering
        filtered_posts_tuple = coarse_filtering(list(t_items.values()), 4000)
        corpus = user_profiles + filtered_posts_tuple[0]

        tweet_vector_start_t = time.time()
        all_post_vector_list = generate_post_vector(corpus, batch_size=1000)
        tweet_vector_end_t = time.time()
        rec_log.info(f"embedding API cost time: {tweet_vector_end_t - tweet_vector_start_t}")

        user_vector = all_post_vector_list[:len(user_profiles)]
        posts_vector = all_post_vector_list[len(user_profiles):]

        if enable_like_score:
            like_posts_vectors = []
            for user_idx, like_post_ids in enumerate(like_post_ids_all):
                if len(like_post_ids) != 1:
                    for like_post_id in like_post_ids:
                        try:
                            like_posts_vectors.append(posts_vector[like_post_id - 1])
                        except Exception:
                            like_posts_vectors.append(user_vector[user_idx])
                else:
                    like_posts_vectors += [user_vector[user_idx] for _ in range(5)]
            like_posts_vectors = np.stack(like_posts_vectors).reshape(
                len(user_table), 5, posts_vector.shape[1])

        get_similar_start_t = time.time()
        cosine_sims = sklearn_cosine_similarity(user_vector, posts_vector)
        get_similar_end_t = time.time()
        rec_log.info(f"get cosine_similarity time: {get_similar_end_t - get_similar_start_t}")

        if enable_like_score:
            for user_index, profile in enumerate(user_profiles):
                user_like_posts_vector = like_posts_vectors[user_index]
                like_scores = calculate_like_similarity(
                    user_like_posts_vector, posts_vector)
                scores = scores + like_scores

        filter_posts_index = np.array(filtered_posts_tuple[1])
        cosine_sims = cosine_sims * scores[filter_posts_index]

        # Top-k per user
        indices = np.argsort(cosine_sims, axis=1)[:, ::-1][:, :max_rec_post_len]
        indices = filter_posts_index[indices]

        post_list = list(t_items.keys())
        for rec_ids in indices:
            rec_ids_mapped = [post_list[i] for i in rec_ids]
            new_rec_matrix.append(rec_ids_mapped)

    return new_rec_matrix


# ─────────────────────────────────────────────────────────────────────
#  Score normalization & swap utilities
# ─────────────────────────────────────────────────────────────────────

def normalize_similarity_adjustments(post_scores, base_similarity,
                                     like_similarity, dislike_similarity):
    if len(post_scores) == 0:
        return base_similarity
    max_score = max(post_scores, key=lambda x: x[1])[1]
    min_score = min(post_scores, key=lambda x: x[1])[1]
    score_range = max_score - min_score
    adjustment = (like_similarity - dislike_similarity) * (score_range / 2)
    return base_similarity + adjustment


def swap_random_posts(rec_post_ids, post_ids, swap_percent=0.1):
    num_to_swap = int(len(rec_post_ids) * swap_percent)
    if num_to_swap == 0 or len(post_ids) < num_to_swap:
        return rec_post_ids
    posts_to_swap = random.sample(post_ids, num_to_swap)
    indices_to_replace = random.sample(range(len(rec_post_ids)), num_to_swap)
    for idx, new_post in zip(indices_to_replace, posts_to_swap):
        rec_post_ids[idx] = new_post
    return rec_post_ids


def get_trace_contents(user_id, action, post_table, trace_table):
    trace_post_ids = [
        trace['post_id'] for trace in trace_table
        if (trace['user_id'] == user_id and trace['action'] == action)
    ]
    trace_contents = [
        post['content'] for post in post_table
        if post['post_id'] in trace_post_ids
    ]
    return trace_contents


# ─────────────────────────────────────────────────────────────────────
#  Personalized recommendation with trace (API version)
# ─────────────────────────────────────────────────────────────────────

def rec_sys_personalized_with_trace(
    user_table: List[Dict[str, Any]],
    post_table: List[Dict[str, Any]],
    trace_table: List[Dict[str, Any]],
    rec_matrix: List[List],
    max_rec_post_len: int,
    swap_rate: float = 0.1,
) -> List[List]:
    """Personalized recommendation with user interaction traces (API-based)."""
    start_time = time.time()
    new_rec_matrix = []
    post_ids = [post['post_id'] for post in post_table]

    if len(post_ids) <= max_rec_post_len:
        new_rec_matrix = [post_ids] * (len(rec_matrix) - 1)
    else:
        # Batch-embed all unique texts for efficiency
        all_bios = [user_table[idx]['bio'] or '' for idx in range(len(user_table))]
        all_post_contents = [post['content'] for post in post_table]
        all_texts = all_bios + all_post_contents
        all_embeddings = generate_post_vector(all_texts, batch_size=1000)

        user_embeddings = all_embeddings[:len(all_bios)]
        post_embeddings = all_embeddings[len(all_bios):]

        for idx in range(1, len(rec_matrix)):
            user_id = user_table[idx - 1]['user_id']
            user_emb = user_embeddings[idx - 1]

            available_post_indices = [
                i for i, post in enumerate(post_table)
                if post['user_id'] != user_id
            ]

            like_trace_contents = get_trace_contents(
                user_id, ActionType.LIKE_POST.value, post_table, trace_table)
            dislike_trace_contents = get_trace_contents(
                user_id, ActionType.UNLIKE_POST.value, post_table, trace_table)

            post_scores = []
            for pi in available_post_indices:
                post_emb = post_embeddings[pi]
                base_sim = float(np.dot(user_emb, post_emb) / (
                    np.linalg.norm(user_emb) * np.linalg.norm(post_emb) + 1e-10))
                post_scores.append((post_table[pi]['post_id'], base_sim))

            # Embed like/dislike traces if needed
            like_embs = None
            dislike_embs = None
            if like_trace_contents:
                like_embs = generate_post_vector(like_trace_contents, batch_size=500)
            if dislike_trace_contents:
                dislike_embs = generate_post_vector(dislike_trace_contents, batch_size=500)

            new_post_scores = []
            for pi_idx, (pid, base_sim) in enumerate(post_scores):
                real_pi = available_post_indices[pi_idx]
                p_emb = post_embeddings[real_pi]

                like_similarity = 0.0
                if like_embs is not None:
                    sims = np.dot(like_embs, p_emb) / (
                        np.linalg.norm(like_embs, axis=1) * np.linalg.norm(p_emb) + 1e-10)
                    like_similarity = float(np.mean(sims))

                dislike_similarity = 0.0
                if dislike_embs is not None:
                    sims = np.dot(dislike_embs, p_emb) / (
                        np.linalg.norm(dislike_embs, axis=1) * np.linalg.norm(p_emb) + 1e-10)
                    dislike_similarity = float(np.mean(sims))

                adjusted = normalize_similarity_adjustments(
                    post_scores, base_sim, like_similarity, dislike_similarity)
                new_post_scores.append((pid, adjusted))

            new_post_scores.sort(key=lambda x: x[1], reverse=True)
            rec_post_ids = [pid for pid, _ in new_post_scores[:max_rec_post_len]]

            if swap_rate > 0:
                swap_free_ids = [
                    pid for pid in post_ids
                    if pid not in rec_post_ids and pid not in [
                        trace['post_id']
                        for trace in trace_table if trace['user_id']
                    ]
                ]
                if swap_free_ids:
                    rec_post_ids = swap_random_posts(rec_post_ids, swap_free_ids,
                                                     swap_rate)

            new_rec_matrix.append(rec_post_ids)

    end_time = time.time()
    print(f'Personalized recommendation time: {end_time - start_time:.6f}s')
    return new_rec_matrix


def get_recsys_model(recsys_type: str = None):
    """Returns None (embedding is handled via API)."""
    return None


def load_model(model_name):
    """Not supported — embedding is handled via API."""
    raise NotImplementedError(
        f"load_model is not supported. (requested: {model_name})")
