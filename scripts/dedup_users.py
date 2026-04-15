"""
One-time script to clean up duplicate users created before email uniqueness was enforced.

Finds groups of `user` documents with the exact same case-insensitive email,
merges their nested profiles/tokens/fields into a single surviving document,
and updates all related schema documents to point to the survivor's `user_id`.

Usage:
  .venv/bin/python scripts/dedup_users.py
"""

import asyncio
import logging

from app.core.database import db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

async def run() -> None:
    # 1. gather duplicate sets (groups of user IDs by lowercased email)
    pipeline = [
        # ignore docs without emails
        {"$match": {"email": {"$exists": True, "$ne": None}}},
        # standardise email casing for grouping
        {"$project": {"email": {"$toLower": "$email"}, "user_id": 1, "is_verified": 1, "google_id": 1, "password": 1, "_id": 1}},
        # group by email, accumulate _ids and user_ids
        {
            "$group": {
                "_id": "$email",
                "docs": {
                    "$push": {
                        "user_id": "$user_id",
                        "_id": "$_id",
                        "is_verified": "$is_verified",
                        "has_google": {"$cond": [{"$ifNull": ["$google_id", False]}, True, False]},
                        "has_password": {"$cond": [{"$ifNull": ["$password", False]}, True, False]}
                    }
                },
                "count": {"$sum": 1}
            }
        },
        # only keep groups with >1 user document
        {"$match": {"count": {"$gt": 1}}}
    ]

    duplicates = await db.users.aggregate(pipeline).to_list(None)

    if not duplicates:
        logger.info("No duplicate email addresses found. Collection is clean.")
        return

    logger.info("Found %d email addresses with duplicate accounts.", len(duplicates))

    total_merged = 0
    total_deleted = 0

    for dup_group in duplicates:
        email = dup_group["_id"]
        docs = dup_group["docs"]

        # sort to pick the best "survivor"
        # priority:
        # 1. verified >> unverified
        # 3. has user_id >> no user_id
        def score(doc):
            return (
                100 * (1 if doc.get("is_verified") else 0)
                + 10 * (1 if doc.get("has_google") else 0)
                + 10 * (1 if doc.get("has_password") else 0)
                + 5 * (1 if doc.get("user_id") else 0)
            )

        docs.sort(key=score, reverse=True)

        survivor = docs[0]
        losers = docs[1:]

        logger.info("Processing email: %s", email)
        logger.info("  Survivor _id: %s, user_id: %s (score: %d)", survivor["_id"], survivor.get("user_id"), score(survivor))

        # merge logic: fetch full docs and fold loser data into survivor
        survivor_full = await db.users.find_one({"_id": survivor["_id"]})
        if not survivor_full:
            logger.error("  Survivor not found in DB? Skipping.")
            continue

        patch = {}
        for loser in losers:
            loser_full = await db.users.find_one({"_id": loser["_id"]})
            if not loser_full:
                continue

            # carry over auth fields if survivor is missing them
            if not survivor_full.get("google_id") and loser_full.get("google_id"):
                patch["google_id"] = loser_full["google_id"]
            if not survivor_full.get("password") and loser_full.get("password"):
                patch["password"] = loser_full["password"]

            # cascading updates: point related docs to survivor user_id
            old_user_id = loser_full.get("user_id")
            new_user_id = survivor_full.get("user_id")

            if old_user_id and new_user_id and old_user_id != new_user_id:
                updated_companies = await db.companies.update_many({"user_id": old_user_id}, {"$set": {"user_id": new_user_id}})
                updated_conversations = await db.conversations.update_many({"user_id": old_user_id}, {"$set": {"user_id": new_user_id}})
                updated_documents = await db.documents.update_many({"user_id": old_user_id}, {"$set": {"user_id": new_user_id}})

                logger.info(
                    "  Reparented %d companies, %d conversations, %d documents from %s to %s",
                    updated_companies.modified_count,
                    updated_conversations.modified_count,
                    updated_documents.modified_count,
                    old_user_id,
                    new_user_id,
                )

            # delete loser
            await db.users.delete_one({"_id": loser["_id"]})
            total_deleted += 1

        # apply patch to survivor if we merged anything
        if patch:
            await db.users.update_one({"_id": survivor["_id"]}, {"$set": patch})
            logger.info("  Patched survivor with inherited auth methods: %s", list(patch.keys()))

        total_merged += 1

    logger.info("Complete! Merged %d groups. Deleted %d duplicate user records.", total_merged, total_deleted)
    logger.info("You can now safely restart the server to build the unique index.")

if __name__ == "__main__":
    asyncio.run(run())
