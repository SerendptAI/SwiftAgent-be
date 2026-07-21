from datetime import datetime, timezone
import re
from urllib.parse import urlparse
from typing import List, Optional
from bson import ObjectId

from app.core.database import db
from app.models.form_models import (
    FormType,
    WebsiteFormCreate,
    OnlineFormCreate,
    FormUpdate,
    FormResponse,
    WebsiteFormCreateResponse,
    OnlineFormCreateResponse,
    FormKeysResponse,
    FormSubmissionCreate,
    FormSubmissionResponse,
    WidgetSubmissionCreate,
    FormGroupInfo,
    PageInfo,
    WebsiteOverview,
    WebsiteDeleteInfo,
    PageDeleteInfo,
    FormDeleteInfo,
    EntryDeleteInfo,
)
from app.services import form_key_service


class FormService:
    # ── Mapping helpers ──

    @staticmethod
    def _map_form(doc: dict) -> FormResponse:
        doc["id"] = str(doc.pop("_id"))
        return FormResponse(**doc)

    @staticmethod
    def _map_submission(doc: dict) -> FormSubmissionResponse:
        doc["id"] = str(doc.pop("_id"))
        return FormSubmissionResponse(**doc)

    @staticmethod
    def extract_submitter_info(data: dict) -> tuple[str | None, str | None]:
        """
        Auto-extract a display name and preview line from form data.

        Given data like {"name": "John Doe", "email": "john@...", "message": "Hello..."},
        returns ("John Doe", "Hello...").
        """
        # Priority for name
        name = None
        name_keys = ["name", "full_name", "fullname", "full name",
                      "your name", "your_name", "contact_name", "sender_name"]
        for key in name_keys:
            for data_key, val in data.items():
                if data_key.lower().replace("-", "_") == key and val:
                    name = str(val).strip()
                    break
            if name:
                break

        # Try first_name + last_name combo
        if not name:
            first = None
            last = None
            for data_key, val in data.items():
                lower = data_key.lower().replace("-", "_")
                if lower in ("first_name", "firstname", "first") and val:
                    first = str(val).strip()
                elif lower in ("last_name", "lastname", "last") and val:
                    last = str(val).strip()
            if first:
                name = f"{first} {last}" if last else first

        # Fall back to email
        if not name:
            for data_key, val in data.items():
                if "email" in data_key.lower() and val:
                    name = str(val).strip()
                    break

        # Priority for preview
        preview = None
        preview_keys = ["message", "body", "comment", "comments", "description",
                        "feedback", "inquiry", "question", "subject", "interest"]
        for key in preview_keys:
            for data_key, val in data.items():
                if data_key.lower().replace("-", "_") == key and val:
                    text = str(val).strip()
                    preview = text[:100] + "..." if len(text) > 100 else text
                    break
            if preview:
                break

        # Fall back to first non-name text field
        if not preview:
            for data_key, val in data.items():
                if data_key.lower() not in ("name", "email", "phone", "tel") and val:
                    text = str(val).strip()
                    if len(text) > 5:
                        preview = text[:100] + "..." if len(text) > 100 else text
                        break

        return name, preview

    @staticmethod
    def _parse_page_path(url: str) -> str:
        """Extract the path portion from a full URL."""
        try:
            parsed = urlparse(url)
            return parsed.path if parsed.path else "/"
        except Exception:
            return "/"

    # ── CRUD: Create ──

    async def create_website_form(self, company_id: str, form_data: WebsiteFormCreate) -> WebsiteFormCreateResponse:
        now = datetime.now(timezone.utc)
        form_doc = {
            "company_id": company_id,
            "type": FormType.WEBSITE,
            "website_link": form_data.website_link,
            "alert_email": form_data.alert_email,
            "tags": form_data.tags or [],
            "created_at": now,
            "updated_at": now,
        }
        res = await db.forms.insert_one(form_doc)
        form_id = str(res.inserted_id)

        # Generate SDPK keys for the widget
        keys = await form_key_service.generate_form_keys(form_id, company_id)
        snippet = form_key_service.generate_snippet(keys["public_key"])

        return WebsiteFormCreateResponse(
            id=form_id,
            company_id=company_id,
            type=FormType.WEBSITE,
            website_link=form_data.website_link,
            alert_email=form_data.alert_email,
            tags=form_data.tags or [],
            created_at=now,
            updated_at=now,
            api_key=keys["api_key"],
            public_key=keys["public_key"],
            snippet=snippet,
        )

    async def create_online_form(self, company_id: str, form_data: OnlineFormCreate) -> OnlineFormCreateResponse:
        now = datetime.now(timezone.utc)
        form_doc = {
            "company_id": company_id,
            "type": FormType.ONLINE,
            "form_image": form_data.form_image,
            "form_title": form_data.form_title,
            "tags": form_data.tags or [],
            "created_at": now,
            "updated_at": now,
        }
        res = await db.forms.insert_one(form_doc)
        form_id = str(res.inserted_id)

        # Generate a shareable URL for the online form
        form_url = f"https://swiftagents.org/forms/{form_id}"

        return OnlineFormCreateResponse(
            id=form_id,
            company_id=company_id,
            type=FormType.ONLINE,
            form_image=form_data.form_image,
            form_title=form_data.form_title,
            tags=form_data.tags or [],
            created_at=now,
            updated_at=now,
            form_url=form_url,
        )

    # ── CRUD: Read ──

    async def get_forms_for_company(self, company_id: str, skip: int = 0, limit: int = 50) -> List[FormResponse]:
        cursor = db.forms.find({"company_id": company_id}).sort("created_at", -1).skip(skip).limit(limit)
        docs = await cursor.to_list(length=limit)
        return [self._map_form(doc) for doc in docs]

    async def get_form_by_id(self, form_id: str) -> Optional[FormResponse]:
        if not ObjectId.is_valid(form_id):
            return None
        doc = await db.forms.find_one({"_id": ObjectId(form_id)})
        if doc:
            return self._map_form(doc)
        return None

    # ── CRUD: Update ──

    async def update_form(self, form_id: str, company_id: str, update_data: FormUpdate) -> Optional[FormResponse]:
        if not ObjectId.is_valid(form_id):
            return None

        update_dict = {k: v for k, v in update_data.model_dump(exclude_unset=True).items() if v is not None}
        if not update_dict:
            return await self.get_form_by_id(form_id)

        update_dict["updated_at"] = datetime.now(timezone.utc)

        result = await db.forms.find_one_and_update(
            {"_id": ObjectId(form_id), "company_id": company_id},
            {"$set": update_dict},
            return_document=True
        )
        if result:
            return self._map_form(result)
        return None

    # ── CRUD: Delete ──

    async def delete_form(self, form_id: str, company_id: str) -> bool:
        if not ObjectId.is_valid(form_id):
            return False
        result = await db.forms.delete_one({"_id": ObjectId(form_id), "company_id": company_id})
        if result.deleted_count > 0:
            # Cascade: delete submissions, keys, and group names
            await db.form_submissions.delete_many({"form_id": form_id, "company_id": company_id})
            await form_key_service.revoke_keys(form_id)
            await db.form_group_names.delete_many({"form_id": form_id})
            return True
        return False

    # ── Widget Submission ──

    async def submit_from_widget(
        self,
        form_id: str,
        company_id: str,
        submission: WidgetSubmissionCreate,
    ) -> FormSubmissionResponse:
        """
        Process a form submission from the embedded widget.

        Stores the submission with page/form context and auto-extracted submitter info.
        """
        now = datetime.now(timezone.utc)
        submitter_name, submitter_preview = self.extract_submitter_info(submission.data)

        # Resolve form_name: use widget-provided name, or check stored custom names,
        # or fall back to the form_identifier
        form_name = submission.form_name
        if not form_name:
            page_path = self._parse_page_path(submission.page_url)
            stored_name = await db.form_group_names.find_one({
                "form_id": form_id,
                "page_path": page_path,
                "form_identifier": submission.form_identifier,
            })
            form_name = stored_name["custom_name"] if stored_name else submission.form_identifier

        doc = {
            "form_id": form_id,
            "company_id": company_id,
            "data": submission.data,
            "is_read": False,
            "visitor_id": submission.visitor_id,
            "submitted_at": now,
            "page_url": submission.page_url,
            "form_identifier": submission.form_identifier,
            "form_name": form_name,
            "submitter_name": submitter_name,
            "submitter_preview": submitter_preview,
        }
        res = await db.form_submissions.insert_one(doc)
        doc["_id"] = res.inserted_id
        return self._map_submission(doc)

    async def submit_form(self, form_id: str, company_id: str, submission_data: FormSubmissionCreate) -> FormSubmissionResponse:
        """Legacy submission (used by online forms and the original public endpoint)."""
        now = datetime.now(timezone.utc)
        submitter_name, submitter_preview = self.extract_submitter_info(submission_data.data)
        doc = {
            "form_id": form_id,
            "company_id": company_id,
            "data": submission_data.data,
            "is_read": False,
            "visitor_id": submission_data.visitor_id,
            "submitted_at": now,
            "submitter_name": submitter_name,
            "submitter_preview": submitter_preview,
        }
        res = await db.form_submissions.insert_one(doc)
        doc["_id"] = res.inserted_id
        return self._map_submission(doc)

    # ── Submission Queries ──

    async def get_submissions_for_form(self, form_id: str, company_id: str, is_read: Optional[bool] = None, skip: int = 0, limit: int = 50) -> List[FormSubmissionResponse]:
        query = {"form_id": form_id, "company_id": company_id}
        if is_read is not None:
            query["is_read"] = is_read

        cursor = db.form_submissions.find(query).sort("submitted_at", -1).skip(skip).limit(limit)
        docs = await cursor.to_list(length=limit)
        return [self._map_submission(doc) for doc in docs]

    async def get_all_submissions_for_company(self, company_id: str, is_read: Optional[bool] = None, skip: int = 0, limit: int = 50) -> List[FormSubmissionResponse]:
        query = {"company_id": company_id}
        if is_read is not None:
            query["is_read"] = is_read

        cursor = db.form_submissions.find(query).sort("submitted_at", -1).skip(skip).limit(limit)
        docs = await cursor.to_list(length=limit)
        return [self._map_submission(doc) for doc in docs]

    async def get_submissions_by_page(
        self, form_id: str, company_id: str, page_path: str,
        is_read: Optional[bool] = None, skip: int = 0, limit: int = 50,
    ) -> List[FormSubmissionResponse]:
        """Get submissions filtered by page path (e.g. /contact-us)."""
        query = {
            "form_id": form_id,
            "company_id": company_id,
            "page_url": {"$regex": f"{page_path}$"},
        }
        if is_read is not None:
            query["is_read"] = is_read

        cursor = db.form_submissions.find(query).sort("submitted_at", -1).skip(skip).limit(limit)
        docs = await cursor.to_list(length=limit)
        return [self._map_submission(doc) for doc in docs]

    async def get_submissions_by_form_group(
        self, form_id: str, company_id: str, page_path: str, form_identifier: str,
        is_read: Optional[bool] = None, skip: int = 0, limit: int = 50,
    ) -> List[FormSubmissionResponse]:
        """Get submissions filtered by specific form within a page."""
        query = {
            "form_id": form_id,
            "company_id": company_id,
            "page_url": {"$regex": f"{page_path}$"},
            "form_identifier": form_identifier,
        }
        if is_read is not None:
            query["is_read"] = is_read

        cursor = db.form_submissions.find(query).sort("submitted_at", -1).skip(skip).limit(limit)
        docs = await cursor.to_list(length=limit)
        return [self._map_submission(doc) for doc in docs]

    async def mark_submission_as_read(self, submission_id: str, company_id: str) -> Optional[FormSubmissionResponse]:
        if not ObjectId.is_valid(submission_id):
            return None

        result = await db.form_submissions.find_one_and_update(
            {"_id": ObjectId(submission_id), "company_id": company_id},
            {"$set": {"is_read": True}},
            return_document=True
        )
        if result:
            return self._map_submission(result)
        return None

    async def get_submission_by_id(self, submission_id: str, company_id: str) -> Optional[FormSubmissionResponse]:
        if not ObjectId.is_valid(submission_id):
            return None
        doc = await db.form_submissions.find_one({"_id": ObjectId(submission_id), "company_id": company_id})
        if doc:
            return self._map_submission(doc)
        return None

    async def add_reply_to_submission(self, submission_id: str, company_id: str, reply: dict) -> Optional[FormSubmissionResponse]:
        if not ObjectId.is_valid(submission_id):
            return None
        result = await db.form_submissions.find_one_and_update(
            {"_id": ObjectId(submission_id), "company_id": company_id},
            {
                "$push": {"replies": reply},
                "$set": {"replied_at": datetime.now(timezone.utc), "is_read": True}
            },
            return_document=True
        )
        if result:
            return self._map_submission(result)
        return None

    async def delete_submission(self, submission_id: str, company_id: str) -> bool:
        if not ObjectId.is_valid(submission_id):
            return False
        result = await db.form_submissions.delete_one({"_id": ObjectId(submission_id), "company_id": company_id})
        return result.deleted_count > 0

    async def delete_submissions_bulk(self, submission_ids: List[str], company_id: str) -> int:
        """Delete multiple submissions by ID."""
        obj_ids = [ObjectId(sid) for sid in submission_ids if ObjectId.is_valid(sid)]
        if not obj_ids:
            return 0
        result = await db.form_submissions.delete_many({
            "_id": {"$in": obj_ids},
            "company_id": company_id,
        })
        return result.deleted_count

    # ── Website Overview & Hierarchy ──

    async def get_website_overview(self, form_id: str, company_id: str) -> Optional[WebsiteOverview]:
        """
        Build a full overview of a website form's auto-discovered pages and forms.

        Groups submissions by page_url → form_identifier to build the hierarchy.
        """
        form = await self.get_form_by_id(form_id)
        if not form or form.company_id != company_id:
            return None

        # Aggregate submissions by page_url and form_identifier
        pipeline = [
            {"$match": {"form_id": form_id, "company_id": company_id}},
            {"$group": {
                "_id": {"page_url": "$page_url", "form_identifier": "$form_identifier"},
                "form_name": {"$last": "$form_name"},
                "entries_count": {"$sum": 1},
                "last_submission": {"$max": "$submitted_at"},
            }},
            {"$sort": {"last_submission": -1}},
        ]
        cursor = db.form_submissions.aggregate(pipeline)
        results = await cursor.to_list(length=None)

        # Group by page path and form identifier
        groups_dict: dict[tuple[str, str], dict] = {}
        for r in results:
            page_url = r["_id"].get("page_url") or ""
            page_path = self._parse_page_path(page_url)
            form_identifier = r["_id"].get("form_identifier") or "default"
            key = (page_path, form_identifier)

            if key not in groups_dict:
                groups_dict[key] = {
                    "form_identifier": form_identifier,
                    "form_name": r.get("form_name") or form_identifier,
                    "entries_count": 0,
                    "last_submission": r.get("last_submission"),
                }
            
            groups_dict[key]["entries_count"] += r.get("entries_count", 0)
            
            curr_last = groups_dict[key]["last_submission"]
            new_last = r.get("last_submission")
            if curr_last and new_last:
                groups_dict[key]["last_submission"] = max(curr_last, new_last)
            elif new_last:
                groups_dict[key]["last_submission"] = new_last

        pages_dict: dict[str, list[FormGroupInfo]] = {}
        for (page_path, form_identifier), data in groups_dict.items():
            # Check for custom name override
            custom_name_doc = await db.form_group_names.find_one({
                "form_id": form_id,
                "page_path": page_path,
                "form_identifier": form_identifier,
            })
            display_name = (
                custom_name_doc["custom_name"]
                if custom_name_doc
                else data["form_name"]
            )

            group = FormGroupInfo(
                form_identifier=form_identifier,
                form_name=display_name,
                entries_count=data["entries_count"],
                last_submission=data["last_submission"],
            )

            if page_path not in pages_dict:
                pages_dict[page_path] = []
            pages_dict[page_path].append(group)

        pages = [
            PageInfo(
                page_path=path,
                forms=groups,
                total_entries=sum(g.entries_count for g in groups),
            )
            for path, groups in pages_dict.items()
        ]
        total = sum(p.total_entries for p in pages)

        return WebsiteOverview(
            form_id=form_id,
            website_link=form.website_link or "",
            pages=pages,
            total_entries=total,
        )

    async def get_pages_for_form(self, form_id: str, company_id: str) -> List[PageDeleteInfo]:
        """List distinct page paths with entry counts (for delete step 2)."""
        pipeline = [
            {"$match": {"form_id": form_id, "company_id": company_id}},
            {"$group": {
                "_id": "$page_url",
                "entries_count": {"$sum": 1},
            }},
            {"$sort": {"_id": 1}},
        ]
        cursor = db.form_submissions.aggregate(pipeline)
        results = await cursor.to_list(length=None)

        return [
            PageDeleteInfo(
                page_path=self._parse_page_path(r["_id"] or ""),
                entries_count=r["entries_count"],
            )
            for r in results
        ]

    async def get_forms_for_page(
        self, form_id: str, company_id: str, page_path: str,
    ) -> List[FormDeleteInfo]:
        """List forms within a page with metadata (for delete step 3)."""
        pipeline = [
            {"$match": {
                "form_id": form_id,
                "company_id": company_id,
                "page_url": {"$regex": re.escape(page_path) + r"([?#].*)?$"},
            }},
            {"$group": {
                "_id": "$form_identifier",
                "form_name": {"$last": "$form_name"},
                "entries_count": {"$sum": 1},
                "last_submission": {"$max": "$submitted_at"},
            }},
            {"$sort": {"last_submission": -1}},
        ]
        cursor = db.form_submissions.aggregate(pipeline)
        results = await cursor.to_list(length=None)

        form_infos = []
        for r in results:
            fi = r["_id"] or "default"
            # Check for custom name override
            custom_name_doc = await db.form_group_names.find_one({
                "form_id": form_id,
                "page_path": page_path,
                "form_identifier": fi,
            })
            display_name = (
                custom_name_doc["custom_name"]
                if custom_name_doc
                else r.get("form_name") or fi
            )
            form_infos.append(FormDeleteInfo(
                form_identifier=fi,
                form_name=display_name,
                entries_count=r["entries_count"],
                last_submission=r.get("last_submission"),
            ))

        return form_infos

    async def get_entries_for_form_group(
        self, form_id: str, company_id: str, page_path: str, form_identifier: str,
        skip: int = 0, limit: int = 50,
    ) -> List[EntryDeleteInfo]:
        """List individual entries within a form group (for delete step 4)."""
        query = {
            "form_id": form_id,
            "company_id": company_id,
            "page_url": {"$regex": re.escape(page_path) + r"([?#].*)?$"},
            "form_identifier": form_identifier,
        }
        cursor = db.form_submissions.find(query).sort("submitted_at", -1).skip(skip).limit(limit)
        docs = await cursor.to_list(length=limit)

        return [
            EntryDeleteInfo(
                id=str(doc["_id"]),
                submitter_name=doc.get("submitter_name"),
                submitter_preview=doc.get("submitter_preview"),
                submitted_at=doc["submitted_at"],
            )
            for doc in docs
        ]

    # ── Hierarchy Delete Operations ──

    async def delete_by_page(self, form_id: str, company_id: str, page_path: str) -> int:
        """Delete all submissions for a page path within a form."""
        result = await db.form_submissions.delete_many({
            "form_id": form_id,
            "company_id": company_id,
            "page_url": {"$regex": re.escape(page_path) + r"([?#].*)?$"},
        })
        return result.deleted_count

    async def delete_by_form_identifier(
        self, form_id: str, company_id: str, page_path: str, form_identifier: str,
    ) -> int:
        """Delete all submissions for a specific form within a page."""
        result = await db.form_submissions.delete_many({
            "form_id": form_id,
            "company_id": company_id,
            "page_url": {"$regex": re.escape(page_path) + r"([?#].*)?$"},
            "form_identifier": form_identifier,
        })
        # Also remove custom name if it exists
        await db.form_group_names.delete_one({
            "form_id": form_id,
            "page_path": page_path,
            "form_identifier": form_identifier,
        })
        return result.deleted_count

    # ── Form Group Rename ──

    async def rename_form_group(
        self, form_id: str, company_id: str, page_path: str, form_identifier: str, new_name: str,
    ) -> bool:
        """Set a custom display name for a detected form (stored in form_group_names)."""
        # Verify form belongs to company
        form = await self.get_form_by_id(form_id)
        if not form or form.company_id != company_id:
            return False

        await db.form_group_names.update_one(
            {
                "form_id": form_id,
                "page_path": page_path,
                "form_identifier": form_identifier,
            },
            {
                "$set": {
                    "custom_name": new_name,
                    "updated_at": datetime.now(timezone.utc),
                },
                "$setOnInsert": {
                    "form_id": form_id,
                    "page_path": page_path,
                    "form_identifier": form_identifier,
                    "company_id": company_id,
                },
            },
            upsert=True,
        )
        return True

    # ── Form Keys ──

    async def get_form_keys(self, form_id: str, company_id: str) -> Optional[FormKeysResponse]:
        """Retrieve key prefixes + snippet for the Security Info tab."""
        form = await self.get_form_by_id(form_id)
        if not form or form.company_id != company_id:
            return None

        keys = await form_key_service.get_keys_for_form(form_id)
        if not keys:
            return None

        # Build a snippet using the prefix (display-only; actual snippet uses full key)
        snippet = form_key_service.generate_snippet(keys["public_key_prefix"] + "XXXXXXXXXXXX")

        return FormKeysResponse(
            form_id=form_id,
            api_key_prefix=keys["api_key_prefix"] + "XXXXXXXXXXXX",
            public_key_prefix=keys["public_key_prefix"] + "XXXXXXXXXXXX",
            snippet=snippet,
        )

    async def regenerate_form_keys(self, form_id: str, company_id: str) -> Optional[dict]:
        """Regenerate keys for a form. Returns full new keys."""
        form = await self.get_form_by_id(form_id)
        if not form or form.company_id != company_id:
            return None
        return await form_key_service.regenerate_keys(form_id, company_id)

    # ── Labels (existing backward-compat) ──

    async def get_labels_for_company(self, company_id: str):
        cursor = db.forms.find({"company_id": company_id})
        docs = await cursor.to_list(length=None)

        website_counts = {}
        page_form_ids = {}

        for doc in docs:
            link = doc.get("website_link")
            if not link:
                continue
            try:
                parsed = urlparse(link)
                website = f"{parsed.scheme}://{parsed.netloc}"
                page = parsed.path if parsed.path else "/"
            except:
                continue

            website_counts[website] = website_counts.get(website, 0) + 1

            page_key = (website, page)
            if page_key not in page_form_ids:
                page_form_ids[page_key] = []
            page_form_ids[page_key].append(str(doc["_id"]))

        websites_list = [{"website": w, "form_count": c} for w, c in website_counts.items()]
        pages_list = []
        for (website, page), form_ids in page_form_ids.items():
            entry_count = await db.form_submissions.count_documents({
                "company_id": company_id,
                "form_id": {"$in": form_ids}
            })
            pages_list.append({
                "website": website,
                "page": page,
                "entry_count": entry_count
            })

        return {
            "websites": websites_list,
            "pages": pages_list
        }

    async def get_websites_for_company(self, company_id: str) -> List[WebsiteDeleteInfo]:
        """List websites with form counts (for delete step 1)."""
        cursor = db.forms.find({"company_id": company_id, "type": FormType.WEBSITE})
        docs = await cursor.to_list(length=None)

        website_counts: dict[str, int] = {}
        for doc in docs:
            link = doc.get("website_link")
            if not link:
                continue
            try:
                parsed = urlparse(link)
                website = f"{parsed.scheme}://{parsed.netloc}"
                website_counts[website] = website_counts.get(website, 0) + 1
            except:
                continue

        return [
            WebsiteDeleteInfo(website=w, form_count=c)
            for w, c in website_counts.items()
        ]

    async def rename_website_label(self, company_id: str, old_website: str, new_website: str) -> int:
        cursor = db.forms.find({"company_id": company_id, "website_link": {"$regex": f"^{old_website}"}})
        docs = await cursor.to_list(length=None)
        updated = 0
        for doc in docs:
            link = doc.get("website_link", "")
            if link.startswith(old_website):
                new_link = new_website + link[len(old_website):]
                await db.forms.update_one(
                    {"_id": doc["_id"]},
                    {"$set": {"website_link": new_link, "updated_at": datetime.now(timezone.utc)}}
                )
                updated += 1
        return updated

    async def rename_page_label(self, company_id: str, website: str, old_page: str, new_page: str) -> int:
        cursor = db.forms.find({"company_id": company_id})
        docs = await cursor.to_list(length=None)
        updated = 0
        for doc in docs:
            link = doc.get("website_link")
            if not link:
                continue
            try:
                parsed = urlparse(link)
                w = f"{parsed.scheme}://{parsed.netloc}"
                p = parsed.path if parsed.path else "/"
                if w == website and p == old_page:
                    new_link = f"{w}{new_page}"
                    if parsed.query:
                        new_link += f"?{parsed.query}"
                    if parsed.fragment:
                        new_link += f"#{parsed.fragment}"

                    await db.forms.update_one(
                        {"_id": doc["_id"]},
                        {"$set": {"website_link": new_link, "updated_at": datetime.now(timezone.utc)}}
                    )
                    updated += 1
            except:
                continue
        return updated

    async def delete_website(self, company_id: str, website: str) -> int:
        cursor = db.forms.find({"company_id": company_id})
        docs = await cursor.to_list(length=None)
        form_ids = []
        for doc in docs:
            link = doc.get("website_link")
            if not link:
                continue
            try:
                parsed = urlparse(link)
                w = f"{parsed.scheme}://{parsed.netloc}"
                if w == website:
                    form_ids.append(str(doc["_id"]))
            except:
                continue
        if form_ids:
            await db.form_submissions.delete_many({"company_id": company_id, "form_id": {"$in": form_ids}})
            # Also revoke keys and clean up group names
            for fid in form_ids:
                await form_key_service.revoke_keys(fid)
                await db.form_group_names.delete_many({"form_id": fid})
            obj_ids = [ObjectId(fid) for fid in form_ids]
            result = await db.forms.delete_many({"company_id": company_id, "_id": {"$in": obj_ids}})
            return result.deleted_count
        return 0

    async def delete_page(self, company_id: str, website: str, page: str) -> int:
        cursor = db.forms.find({"company_id": company_id})
        docs = await cursor.to_list(length=None)
        form_ids = []
        for doc in docs:
            link = doc.get("website_link")
            if not link:
                continue
            try:
                parsed = urlparse(link)
                w = f"{parsed.scheme}://{parsed.netloc}"
                p = parsed.path if parsed.path else "/"
                if w == website and p == page:
                    form_ids.append(str(doc["_id"]))
            except:
                continue
        if form_ids:
            await db.form_submissions.delete_many({"company_id": company_id, "form_id": {"$in": form_ids}})
            for fid in form_ids:
                await form_key_service.revoke_keys(fid)
                await db.form_group_names.delete_many({"form_id": fid})
            obj_ids = [ObjectId(fid) for fid in form_ids]
            result = await db.forms.delete_many({"company_id": company_id, "_id": {"$in": obj_ids}})
            return result.deleted_count
        return 0

form_service = FormService()
