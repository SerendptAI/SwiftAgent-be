from datetime import datetime, timezone
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
    FormSubmissionCreate,
    FormSubmissionResponse
)

class FormService:
    @staticmethod
    def _map_form(doc: dict) -> FormResponse:
        doc["id"] = str(doc.pop("_id"))
        return FormResponse(**doc)

    @staticmethod
    def _map_submission(doc: dict) -> FormSubmissionResponse:
        doc["id"] = str(doc.pop("_id"))
        return FormSubmissionResponse(**doc)

    async def create_website_form(self, company_id: str, form_data: WebsiteFormCreate) -> FormResponse:
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
        form_doc["_id"] = res.inserted_id
        return self._map_form(form_doc)

    async def create_online_form(self, company_id: str, form_data: OnlineFormCreate) -> FormResponse:
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
        form_doc["_id"] = res.inserted_id
        return self._map_form(form_doc)

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

    async def delete_form(self, form_id: str, company_id: str) -> bool:
        if not ObjectId.is_valid(form_id):
            return False
        result = await db.forms.delete_one({"_id": ObjectId(form_id), "company_id": company_id})
        if result.deleted_count > 0:
            # Also delete submissions
            await db.form_submissions.delete_many({"form_id": form_id, "company_id": company_id})
            return True
        return False

    async def submit_form(self, form_id: str, company_id: str, submission_data: FormSubmissionCreate) -> FormSubmissionResponse:
        now = datetime.now(timezone.utc)
        doc = {
            "form_id": form_id,
            "company_id": company_id,
            "data": submission_data.data,
            "is_read": False,
            "visitor_id": submission_data.visitor_id,
            "submitted_at": now
        }
        res = await db.form_submissions.insert_one(doc)
        doc["_id"] = res.inserted_id
        return self._map_submission(doc)

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
            obj_ids = [ObjectId(fid) for fid in form_ids]
            result = await db.forms.delete_many({"company_id": company_id, "_id": {"$in": obj_ids}})
            return result.deleted_count
        return 0

form_service = FormService()


