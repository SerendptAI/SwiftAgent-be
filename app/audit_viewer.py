"""
Streamlit Audit Log Viewer for SwiftAgent-be.

Run with: streamlit run app/audit_viewer.py
"""

import streamlit as st
import pandas as pd
from datetime import datetime, timedelta, timezone
from app.core.database import db
from app.core.audit import AuditLogger, query_audit_events

st.set_page_config(page_title="Audit Log", page_icon="📋", layout="wide")

st.title("📋 Audit Log Viewer")
st.markdown("View and filter all audited events in the system.")

# ── Filters ──────────────────────────────────────────────────────────────────

col1, col2, col3, col4 = st.columns(4)

with col1:
    company_id = st.text_input("Company ID (optional)", "")

with col2:
    actor_id = st.text_input("Actor ID (optional)", "")

with col3:
    resource_type = st.selectbox(
        "Resource Type",
        ["All", "ticket", "conversation", "user", "knowledge", "company", "form", "http_request"],
    )

with col4:
    action = st.selectbox(
        "Action",
        ["All", "read", "write", "delete", "export", "login", "invite", "role_change", "suspend", "reactivate", "remove"],
    )

col5, col6 = st.columns(2)

with col5:
    date_range = st.selectbox(
        "Date Range",
        ["Last 24 hours", "Last 7 days", "Last 30 days", "Last 90 days", "All time"],
    )

with col6:
    status_filter = st.selectbox("Status", ["All", "success", "denied", "error"])

# ── Query ────────────────────────────────────────────────────────────────────

# Calculate date range
now = datetime.now(timezone.utc)
date_ranges = {
    "Last 24 hours": now - timedelta(hours=24),
    "Last 7 days": now - timedelta(days=7),
    "Last 30 days": now - timedelta(days=30),
    "Last 90 days": now - timedelta(days=90),
    "All time": None,
}
start_date = date_ranges[date_range]

# Build query
filters = {}
if company_id:
    filters["company_id"] = company_id
if actor_id:
    filters["actor_id"] = actor_id
if resource_type != "All":
    filters["resource_type"] = resource_type
if action != "All":
    filters["action"] = action
if status_filter != "All":
    filters["status"] = status_filter
if start_date:
    filters["start_date"] = start_date

# Execute query
@st.cache_data(ttl=60)
def get_events(filters_dict, limit=200):
    import asyncio
    return asyncio.run(query_audit_events(limit=limit, **filters_dict))

events = get_events(filters)

# ── Display ──────────────────────────────────────────────────────────────────

st.markdown(f"**{len(events)}** events found")

if events:
    # Convert to DataFrame for display
    df = pd.DataFrame(events)
    
    # Select and rename columns
    display_cols = ["timestamp", "actor_email", "actor_role", "resource_type", "resource_id", "action", "status", "changes"]
    available_cols = [c for c in display_cols if c in df.columns]
    df = df[available_cols]
    
    # Format timestamp
    if "timestamp" in df.columns:
        df["timestamp"] = pd.to_datetime(df["timestamp"]).dt.strftime("%Y-%m-%d %H:%M:%S")
    
    st.dataframe(df, use_container_width=True, height=600)
    
    # Export
    csv = df.to_csv(index=False)
    st.download_button(
        "📥 Export to CSV",
        csv,
        "audit_log.csv",
        "text/csv",
    )
else:
    st.info("No events found matching the filters.")

# ── Stats ────────────────────────────────────────────────────────────────────

st.markdown("---")
st.subheader("📊 Quick Stats")

col1, col2, col3, col4 = st.columns(4)

with col1:
    st.metric("Total Events", len(events))

with col2:
    success_count = sum(1 for e in events if e.get("status") == "success")
    st.metric("Success", success_count)

with col3:
    denied_count = sum(1 for e in events if e.get("status") == "denied")
    st.metric("Denied", denied_count)

with col4:
    error_count = sum(1 for e in events if e.get("status") == "error")
    st.metric("Errors", error_count)
