#!/usr/bin/env python3
"""
Google Sheets to Hugo Post Generator for The Gayslist

Idempotently generate hugo markdown posts from approved google sheets rows.
Workflow:
1. Users submit via Google Form
2. Moderators review/approve in Google Sheet
3. This script generates Hugo posts for ALL approved entries
4. Hugo builds and deploys the site in CI
"""

import argparse
import json
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import gspread
from google.oauth2.service_account import Credentials
from pydantic import ConfigDict, Field
from pydantic_settings import BaseSettings

AUTO_GENERATED_FILE_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}-"
)

class Settings(BaseSettings):
    google_credentials_json: str = Field(default="", alias="GOOGLE_CREDENTIALS_JSON")
    google_sheet_id: str = Field(default="", alias="GOOGLE_SHEET_ID")
    content_dir: str = Field(default="content/post", alias="CONTENT_DIR")

    model_config = ConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )  # type: ignore[call-arg]


settings = Settings()


def row_to_string(row: dict[Any, Any], key: str, default: str = "") -> str:
    """Safely extract a sheet value as a stripped string."""
    return str(row.get(key, default)).strip()


def authenticate_sheets() -> gspread.Client:
    if not settings.google_credentials_json:
        raise ValueError("GOOGLE_CREDENTIALS_JSON env var not set")
    credentials_dict = json.loads(settings.google_credentials_json)
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds: Credentials = Credentials.from_service_account_info(credentials_dict, scopes=scopes)  # type: ignore[no-untyped-call]
    return gspread.authorize(creds)


def get_worksheet(client: gspread.Client, sheet_id: str, index: int = 0) -> gspread.Worksheet:
    return client.open_by_key(sheet_id).get_worksheet(index)


def sanitize_text(text: str) -> str:
    """Convert text to filename-safe string."""
    return re.sub(r"[-\s]+", "-", re.sub(r"[^\w\s-]", "", text.lower())).strip("-")


def parse_submission_timestamp(raw: str) -> str:
    """Parse Google Forms timestamp into ISO 8601. Fall back to now()."""
    for fmt in ("%m/%d/%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%d/%m/%Y %H:%M:%S"):
        try:
            dt = datetime.strptime(raw.strip(), fmt).replace(tzinfo=UTC)
            return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            continue
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_filename_for_row(row: dict[Any, Any]) -> str:
    """Derive the deterministic filename for a row without generating full content."""
    title = row_to_string(row, "Listing Title", "Untitled Post")
    timestamp = row_to_string(row, "Timestamp")
    post_uuid = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{timestamp}:{title}"))
    title_slug = sanitize_text(title)
    return f"{post_uuid}-{title_slug}" if title_slug else post_uuid


def generate_hugo_post(row: dict[Any, Any]) -> tuple[str, str]:
    """Generate a Hugo post from a sheet row. Returns (filename, content)."""
    title = row_to_string(row, "Listing Title", "Untitled Post")
    description = row_to_string(row, "Listing Description")
    category = row_to_string(row, "Category:", "for-sale").lower().replace(" ", "-")
    tags_str = row_to_string(row, "Tags (comma separated list)")
    price = row_to_string(row, "Price:")
    location = row_to_string(row, "Location")
    condition = row_to_string(row, "Condition", "N/A")
    contact_method = row_to_string(row, "Contact Method", "email")
    contact_info = row_to_string(row, "Contact Info")
    email = row_to_string(row, "Email Address")
    timestamp = row_to_string(row, "Timestamp")

    post_uuid = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{timestamp}:{title}"))

    tags = sorted({tag.strip().lower() for tag in tags_str.split(",") if tag.strip()})
    title_slug = sanitize_text(title)
    filename = f"{post_uuid}-{title_slug}" if title_slug else post_uuid

    post_date = parse_submission_timestamp(timestamp)
    categories_toml = "[" + ", ".join(f'"{cat}"' for cat in (category,)) + "]"
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

    return filename, front_matter + "\n\n" + "\n".join(content_sections)


def save_hugo_post(filename: str, content: str, hugo_content_dir: str) -> str:
    if not filename.endswith(".md"):
        filename += ".md"
    output_dir = Path(hugo_content_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    filepath = output_dir / filename
    filepath.write_text(content, encoding="utf-8")
    return str(filepath)


def delete_hugo_post(filename: str, hugo_content_dir: str) -> bool:
    """Delete a Hugo post file if it exists. Returns True if a file was removed."""
    if not filename.endswith(".md"):
        filename += ".md"
    filepath = Path(hugo_content_dir) / filename
    if filepath.exists():
        filepath.unlink()
        return True
    return False


def mark_published_in_sheet(
    worksheet: gspread.Worksheet, row_index: int, headers: list[str]
) -> bool:
    """Mark a single row as published. Expects the 1-based row index."""
    if "published" not in headers:
        return False
    try:
        col_idx = headers.index("published") + 1
        worksheet.update_cell(row_index, col_idx, "TRUE")
        return True
    except Exception as e:
        print(f"Warning: Could not mark row {row_index} as published: {e}")
        return False


def mark_unpublished_in_sheet(
    worksheet: gspread.Worksheet, row_index: int, headers: list[str]
) -> bool:
    """Mark a single row as unpublished. Expects the 1-based row index."""
    if "published" not in headers:
        return False
    try:
        col_idx = headers.index("published") + 1
        worksheet.update_cell(row_index, col_idx, "FALSE")
        return True
    except Exception as e:
        print(f"Warning: Could not mark row {row_index} as unpublished: {e}")
        return False


def clear_rerender_flag(worksheet: gspread.Worksheet, row_index: int, headers: list[str]) -> bool:
    """Reset re_render_post to FALSE after processing."""
    if "re_render_post" not in headers:
        return False
    try:
        col_idx = headers.index("re_render_post") + 1
        worksheet.update_cell(row_index, col_idx, "FALSE")
        return True
    except Exception as e:
        print(f"Warning: Could not clear re_render_post for row {row_index}: {e}")
        return False


def handle_approved_row(
    row: dict[Any, Any],
    row_index: int,
    worksheet: gspread.Worksheet,
    headers: list[str],
    hugo_content_dir: str,
    dry_run: bool = False,
) -> str:
    """Process an approved row. Returns the filepath/filename."""
    filename, content = generate_hugo_post(row)

    if dry_run:
        print(f"[DRY RUN] Would write: {filename}.md")
        print(f"   Title: {row_to_string(row, 'Listing Title')}\n")
        return filename

    filepath = save_hugo_post(filename, content, hugo_content_dir)
    print(f"Wrote post: {filepath}")

    if mark_published_in_sheet(worksheet, row_index, headers):
        print("   Marked as published in sheet")
    if clear_rerender_flag(worksheet, row_index, headers):
        print("   Cleared re_render_post flag")

    return filepath


def handle_rejected_row(
    row: dict[Any, Any],
    row_index: int,
    worksheet: gspread.Worksheet,
    headers: list[str],
    hugo_content_dir: str,
    dry_run: bool = False,
) -> str | None:
    """Process a rejected row. Returns the filename if a post was deleted."""
    filename = get_filename_for_row(row)

    if dry_run:
        print(f"[DRY RUN] Would delete: {filename}.md")
        return filename

    deleted = delete_hugo_post(filename, hugo_content_dir)
    if deleted:
        print(f"Deleted post: {filename}.md")

    if mark_unpublished_in_sheet(worksheet, row_index, headers):
        print("   Marked as unpublished in sheet")
    if clear_rerender_flag(worksheet, row_index, headers):
        print("   Cleared re_render_post flag")

    return filename if deleted else None


def cleanup_orphaned_posts(
    expected_filenames: set[str],
    hugo_content_dir: str,
    dry_run: bool = False,
) -> list[str]:
    """Delete auto-generated posts that no longer correspond to a sheet row.
    Static posts (filenames without a UUID prefix) are never touched."""
    content_dir = Path(hugo_content_dir)
    if not content_dir.exists():
        return []

    orphaned: list[str] = []
    for filepath in content_dir.glob("*.md"):
        filename = filepath.stem  # without .md

        # Skip posts we made by hand (i.e. no UUID-title file name)
        if not AUTO_GENERATED_FILE_PATTERN.match(filename):
            continue

        if filename in expected_filenames:
            continue

        if dry_run:
            print(f"[DRY RUN] Would delete orphan: {filepath.name}")
        else:
            filepath.unlink()
            print(f"Deleted orphaned post: {filepath.name}")
        orphaned.append(str(filepath))

    return orphaned


def process_posts(
    sheet_id: str,
    hugo_content_dir: str = "content/post",
    dry_run: bool = False,
) -> tuple[list[str], list[str]]:
    try:
        client = authenticate_sheets()
        worksheet = get_worksheet(client, sheet_id)
        headers = worksheet.row_values(1)
        records = worksheet.get_all_records()
    except Exception as e:
        print(f"Error fetching from Google Sheet: {e}")
        return [], []

    created_posts: list[str] = []
    deleted_posts: list[str] = []

    for row_index, row in enumerate(records, start=2):
        status = row_to_string(row, "moderation_status").lower()

        try:
            match status:
                case "approved":
                    result = handle_approved_row(
                        row, row_index, worksheet, headers, hugo_content_dir, dry_run
                    )
                    created_posts.append(result)

                case "rejected":
                    result = handle_rejected_row(
                        row, row_index, worksheet, headers, hugo_content_dir, dry_run
                    )
                    if result:
                        deleted_posts.append(result)

                case "pending":
                    pass

                case _:
                    print(
                        f"Warning: Unknown moderation_status '{status}' for row {row_index}, skipping"
                    )

        except Exception as e:
            timestamp = row_to_string(row, "Timestamp") or "unknown"
            print(f"Error processing row {timestamp}: {e}")

    # Build set of filenames that should exist (approved rows)
    expected_filenames: set[str] = set()
    for _, row in enumerate(records, start=2):
        if row_to_string(row, "moderation_status").lower() == "approved":
            expected_filenames.add(get_filename_for_row(row))

    orphaned_posts = cleanup_orphaned_posts(expected_filenames, hugo_content_dir, dry_run)
    deleted_posts.extend(orphaned_posts)

    return created_posts, deleted_posts


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate Hugo posts from approved Google Sheet entries"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Show what would be created without writing files"
    )
    args = parser.parse_args()

    if not settings.google_sheet_id:
        print("Error: GOOGLE_SHEET_ID not set")
        return 1

    print("Google Sheets to Hugo Post Generator\n")
    print(f"Using Sheet: {settings.google_sheet_id}")
    print(f"Content Dir: {settings.content_dir}\n")

    created_posts, deleted_posts = process_posts(
        sheet_id=settings.google_sheet_id,
        hugo_content_dir=settings.content_dir,
        dry_run=args.dry_run,
    )

    if args.dry_run:
        print(
            f"\n[DRY RUN] Would have written {len(created_posts)} posts, deleted {len(deleted_posts)} posts"
        )
    else:
        if created_posts:
            print(f"\nSuccessfully wrote {len(created_posts)} posts:")
            for post in created_posts:
                print(f"  - {post}")
        if deleted_posts:
            print(f"\nDeleted {len(deleted_posts)} posts:")
            for post in deleted_posts:
                print(f"  - {post}")

    if not created_posts and not deleted_posts:
        print("\nNo changes to process")
    return 0


if __name__ == "__main__":
    exit(main())
