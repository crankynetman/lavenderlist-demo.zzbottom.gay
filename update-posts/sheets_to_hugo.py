#!/usr/bin/env python3
"""
Google Sheets to Hugo Post Generator for The Gayslist

This script reads approved posts from a Google Sheet and generates Hugo posts automatically.
Designed for: Google Form -> Google Sheet for moderation -> Row data rendered to markdown ->
markdown rendered by Hugo into HTML Posts -> published w/ GH Actions.

Workflow:
1. Users submit via Google Form
2. Moderators review/approve in Google Sheet
3. This script generates Hugo posts for ALL approved entries (idempotently)
4. Hugo builds and deploys the site in CI.
"""

import argparse
import json
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path

import gspread
from google.oauth2.service_account import Credentials
from pydantic import ConfigDict, Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    google_credentials_json: str = Field(default="", alias="GOOGLE_CREDENTIALS_JSON")
    google_sheet_id: str = Field(default="", alias="GOOGLE_SHEET_ID")
    content_dir: str = Field(default="content/post", alias="CONTENT_DIR")

    model_config = ConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )


settings = Settings()


def row_to_string(row_data: dict, key: str, default: str = "") -> str:
    """Safely extract a sheet value as a stripped string."""
    val = row_data.get(key, default)
    return str(val).strip()


def authenticate_sheets() -> gspread.Client:
    if not settings.google_credentials_json:
        raise ValueError("GOOGLE_CREDENTIALS_JSON env var not set")

    credentials_dict = json.loads(settings.google_credentials_json)
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_info(credentials_dict, scopes=scopes)
    return gspread.authorize(creds)


def fetch_approved_posts_from_sheet(sheet_id: str) -> list[dict]:
    """Fetch all approved rows. No published/re-render flags — every approved row is rendered."""
    client = authenticate_sheets()
    try:
        sheet = client.open_by_key(sheet_id)
        worksheet = sheet.get_worksheet(0)
        records = worksheet.get_all_records()

        return [
            row for row in records if row_to_string(row, "moderation_status").lower() == "approved"
        ]
    except Exception as e:
        print(f"Error fetching from Google Sheet: {e}")
        return []


def sanitize_text(text: str) -> str:
    """Convert text to valid filename-safe string."""
    return re.sub(r"[-\s]+", "-", re.sub(r"[^\w\s-]", "", text.lower())).strip("-")


def parse_submission_timestamp(raw: str) -> str:
    """Try to parse the Google Forms timestamp into ISO 8601. Fall back to now()."""
    formats = [
        "%m/%d/%Y %H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y/%m/%d %H:%M:%S",
        "%d/%m/%Y %H:%M:%S",
    ]
    for fmt in formats:
        try:
            dt = datetime.strptime(raw.strip(), fmt).replace(tzinfo=UTC)
            return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            continue
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def generate_hugo_post_from_sheet_row(row_data: dict) -> tuple[str, str, str]:
    """Returns (filename, content, post_uuid)."""
    title = row_to_string(row_data, "Listing Title", "Untitled Post")
    description = row_to_string(row_data, "Listing Description")
    category = row_to_string(row_data, "Category:", "for-sale").lower().replace(" ", "-")
    tags_str = row_to_string(row_data, "Tags (comma separated list)")
    price = row_to_string(row_data, "Price:")
    location = row_to_string(row_data, "Location")
    condition = row_to_string(row_data, "Condition", "N/A")
    contact_method = row_to_string(row_data, "Contact Method", "email")
    contact_info = row_to_string(row_data, "Contact Info")
    email = row_to_string(row_data, "Email Address")
    timestamp = row_to_string(row_data, "Timestamp")

    post_uuid = row_to_string(row_data, "uuid")

    if not post_uuid:
        post_uuid = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{timestamp}:{title}"))

    tags = [tag.strip().lower() for tag in tags_str.split(",") if tag.strip()]
    title_slug = sanitize_text(title)
    filename = f"{post_uuid}-{title_slug}" if title_slug else post_uuid

    categories = [category]
    post_date = parse_submission_timestamp(timestamp)
    categories_toml = "[" + ", ".join(f'"{cat}"' for cat in categories) + "]"
    tags_toml = "[" + ", ".join(f'"{tag}"' for tag in tags) + "]"

    front_matter = f"""+++
date = "{post_date}"
draft = false
title = "{title}"
categories = {categories_toml}
tags = {tags_toml}
price = "{price}"
location = "{location}"
contact_method = "{contact_method}"
contact_info = "{contact_info}"
condition = "{condition}"
post_uuid = "{post_uuid}"
submitter_email = "{email}"
+++"""

    content_sections = [f"## {title}"]

    if price:
        content_sections.append(f"**Price:** {price}")
    if location:
        content_sections.append(f"**Location:** {location}")
    if condition and condition.lower() != "n/a":
        content_sections.append(f"**Condition:** {condition}")

    content_sections.append("")
    if description:
        content_sections.extend(("### Description", description, ""))

    content_sections.append("### Contact")
    if contact_method:
        content_sections.append(f"**Preferred Contact:** {contact_method.title()}")
    if contact_info:
        content_sections.append(f"**Contact Details:** {contact_info}")
    content_sections.extend(
        ("Please reach out with any questions!", "", "**Thanks for supporting our community! 🏳️‍🌈**")
    )

    return filename, front_matter + "\n\n" + "\n".join(content_sections), post_uuid


def save_hugo_post(filename: str, content: str, hugo_content_dir: str = "content/post") -> str:
    if not filename.endswith(".md"):
        filename += ".md"

    filepath = Path(hugo_content_dir) / filename
    Path(hugo_content_dir).mkdir(parents=True, exist_ok=True)
    filepath.write_text(content, encoding="utf-8")
    return str(filepath)


def update_uuid_in_sheet(
    sheet_id: str, row_data: dict, post_uuid: str, worksheet_index: int = 0
) -> bool:
    try:
        client = authenticate_sheets()
        sheet = client.open_by_key(sheet_id)
        worksheet = sheet.get_worksheet(worksheet_index)
        timestamp = row_to_string(row_data, "Timestamp")
        records = worksheet.get_all_records()

        for idx, record in enumerate(records, start=2):
            if row_to_string(record, "Timestamp") == timestamp:
                headers = worksheet.row_values(1)
                if "uuid" in headers:
                    col_idx = headers.index("uuid") + 1
                    worksheet.update_cell(idx, col_idx, post_uuid)
                    return True
        return False
    except Exception as e:
        print(f"Warning: Could not update UUID in sheet: {e}")
        return False


def process_approved_posts_from_sheet(
    sheet_id: str,
    hugo_content_dir: str = "content/post",
    dry_run: bool = False,
) -> tuple[list[str], int]:
    posts = fetch_approved_posts_from_sheet(sheet_id)
    created_posts = []

    for row in posts:
        try:
            filename, content, post_uuid = generate_hugo_post_from_sheet_row(row)
            filepath = f"{hugo_content_dir}/{filename}.md"

            if dry_run:
                print(f"[DRY RUN] Would write: {filename}.md")
                print(f"   Title: {row.get('Listing Title')}")
                print(f"   UUID: {post_uuid}")
                print()
                created_posts.append(filepath)
            else:
                filepath = save_hugo_post(filename, content, hugo_content_dir)
                created_posts.append(filepath)
                print(f"Wrote post: {filepath}")

                if update_uuid_in_sheet(sheet_id, row, post_uuid):
                    print(f"   UUID saved: {post_uuid}")

        except Exception as e:
            timestamp = row_to_string(row, "Timestamp") or "unknown"
            print(f"Error processing row {timestamp}: {e}")

    return created_posts, len(created_posts)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate Hugo posts from approved Google Sheet entries"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be created without writing files",
    )
    args = parser.parse_args()

    if not settings.google_sheet_id:
        print("Error: GOOGLE_SHEET_ID not set")
        return 1

    print("Google Sheets to Hugo Post Generator\n")
    print(f"Using Sheet: {settings.google_sheet_id}")
    print(f"Content Dir: {settings.content_dir}\n")

    created_posts, count = process_approved_posts_from_sheet(
        sheet_id=settings.google_sheet_id,
        hugo_content_dir=settings.content_dir,
        dry_run=args.dry_run,
    )

    if args.dry_run:
        print(f"\n[DRY RUN] Would have written {count} posts")
    else:
        print(f"\nSuccessfully wrote {count} posts:")
        for post in created_posts:
            print(f"  - {post}")

    if count == 0:
        print("\nNo approved posts to process")
    return 0


if __name__ == "__main__":
    exit(main())
