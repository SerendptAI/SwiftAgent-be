# SwiftForms Real-Time WebSocket API Handoff

This document details the real-time WebSocket endpoints added to the **SwiftForms** module for live updates on form creations, submissions, and website overviews in the dashboard.

---

## 1. Authentication

All WebSocket endpoints require authentication via a valid **user JWT access token** passed in the URL query string:
```
?token=<YOUR_JWT_ACCESS_TOKEN>
```
If the token is missing, expired, or the user is not authorized for the requested `company_id`, the WebSocket connection will immediately emit an error JSON message and close with status code `1008 (Policy Violation)`.

---

## 2. Real-Time Endpoints

### A. All Forms List (Company Level)
* **Endpoint:** `GET /api/v1/forms/{company_id}/ws`
* **Protocol:** `ws://` / `wss://`
* **Description:** Emits real-time updates whenever forms are added, updated, or deleted for the company.
* **Payload format:**
  ```json
  {
    "items": [
      {
        "id": "64bc...a1b2",
        "company_id": "com_123",
        "type": "website",
        "tags": [],
        "is_active": true,
        "website_link": "https://example.com",
        "created_at": "2026-07-26T14:10:00Z",
        "updated_at": "2026-07-26T14:10:00Z"
      }
    ],
    "total": 1
  }
  ```

---

### B. All Form Submissions (Company Level)
* **Endpoint:** `GET /api/v1/forms/{company_id}/submissions/ws`
* **Protocol:** `ws://` / `wss://`
* **Description:** Emits a real-time list of the latest 50 form submissions across **all forms** in the company. Automatically pushes a new list whenever any submission is added, updated, or deleted.
* **Payload format:**
  ```json
  {
    "items": [
      {
        "id": "64bc...e9f0",
        "form_id": "64bc...a1b2",
        "company_id": "com_123",
        "form_name": "Contact Form",
        "page_url": "https://example.com/contact",
        "data": {
          "name": "Jane Doe",
          "email": "jane@example.com",
          "message": "Hello world"
        },
        "is_read": false,
        "submitted_at": "2026-07-26T14:10:00Z"
      }
    ],
    "total": 120,
    "limit": 50,
    "skip": 0,
    "has_next": true
  }
  ```

---

### C. Submissions for a Specific Form
* **Endpoint:** `GET /api/v1/forms/{company_id}/{form_id}/submissions/ws`
* **Protocol:** `ws://` / `wss://`
* **Description:** Emits real-time updates for submissions belonging to a specific form (`form_id`).
* **Payload format:**
  ```json
  {
    "items": [ ... ],
    "total": 45,
    "limit": 50,
    "skip": 0,
    "has_next": false
  }
  ```

---

### D. Website Overview (Specific Form/Website)
* **Endpoint:** `GET /api/v1/forms/{company_id}/{form_id}/overview/ws`
* **Protocol:** `ws://` / `wss://`
* **Description:** Emits the full aggregated `WebsiteOverview` object (pages, forms, submission stats) upon connection and whenever submissions or forms under this website change.
* **Payload format:**
  ```json
  {
    "form_id": "64bc...a1b2",
    "website_link": "https://example.com",
    "total_entries": 45,
    "pages": [
      {
        "page_path": "/contact",
        "total_entries": 45,
        "forms": [
          {
            "form_identifier": "id:contact-form",
            "form_name": "Contact Form",
            "entries_count": 45,
            "last_submission": "2026-07-26T14:10:00Z"
          }
        ]
      }
    ]
  }
  ```

---

## 3. Frontend Integration Snippet (React / TS / JS)

Below is an example React hook / utility snippet for integrating the submissions live stream:

```typescript
import { useEffect, useState } from "react";

export function useRealtimeSubmissions(companyId: string, token: string) {
  const [submissions, setSubmissions] = useState<any[]>([]);
  const [total, setTotal] = useState<number>(0);

  useEffect(() => {
    if (!companyId || !token) return;

    const wsUrl = `wss://api.yourdomain.com/api/v1/forms/${companyId}/submissions/ws?token=${encodeURIComponent(token)}`;
    const ws = new WebSocket(wsUrl);

    ws.onmessage = (event) => {
      const data = JSON.parse(event.data);
      if (data && data.items) {
        setSubmissions(data.items);
        setTotal(data.total);
      }
    };

    ws.onerror = (error) => {
      console.error("[SwiftForms WS] Connection error:", error);
    };

    return () => {
      ws.close();
    };
  }, [companyId, token]);

  return { submissions, total };
}
```
