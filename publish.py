#!/usr/bin/env python3
"""Publish one queued single-image post to Instagram via the Graph API."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import requests

ROOT = Path(__file__).resolve().parent
QUEUE_PATH = ROOT / "content" / "queue.json"
STATE_PATH = ROOT / "state" / "published.json"
IST = ZoneInfo("Asia/Kolkata")
SCHEDULE_TO_SLOT = {
    "30 2 * * *": "08:00",
    "30 7 * * *": "13:00",
    "30 14 * * *": "20:00",
}
HTTP_TIMEOUT = 30


class PublishError(RuntimeError):
    pass


def load_json(path: Path, default):
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    tmp.replace(path)


def resolve_slot(args) -> str:
    if args.slot:
        slot = args.slot
    else:
        schedule = os.getenv("GITHUB_EVENT_SCHEDULE", "")
        slot = SCHEDULE_TO_SLOT.get(schedule, "")
    if slot not in {"08:00", "13:00", "20:00"}:
        raise PublishError(
            "Could not determine slot. Use workflow_dispatch with slot 08:00, 13:00, or 20:00."
        )
    return slot


def build_image_url(base_url: str, image_path: str) -> str:
    if not base_url:
        raise PublishError("PUBLIC_IMAGE_BASE_URL is not set.")
    parsed = urlparse(base_url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise PublishError("PUBLIC_IMAGE_BASE_URL must be an HTTPS URL.")
    # Prevent queue paths from escaping the repository's public asset path.
    relative = image_path.lstrip("/")
    if ".." in Path(relative).parts:
        raise PublishError(f"Unsafe image path in queue: {image_path}")
    return base_url.rstrip("/") + "/" + relative


def verify_public_jpeg(image_url: str):
    """Check that Meta can reach a public JPEG URL before attempting a publish."""
    try:
        response = requests.head(image_url, allow_redirects=True, timeout=HTTP_TIMEOUT)
        # Some hosts reject HEAD even though GET works; retry with a streamed GET.
        if response.status_code in (403, 405, 501):
            response.close()
            response = requests.get(image_url, allow_redirects=True, stream=True, timeout=HTTP_TIMEOUT)
        if not response.ok:
            raise PublishError(f"Image URL check failed with HTTP {response.status_code}: {image_url}")
        content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type and content_type not in {"image/jpeg", "image/jpg"}:
            raise PublishError(f"Image URL must return JPEG content, got '{content_type}': {image_url}")
        response.close()
    except requests.RequestException as exc:
        raise PublishError(f"Could not verify public image URL: {exc}") from exc


def choose_post(slot: str, queue: list, published_ids: set) -> dict | None:
    for post in queue:
        if not isinstance(post, dict):
            continue
        if post.get("enabled", True) is not True:
            continue
        if post.get("slot") != slot:
            continue
        post_id = str(post.get("id", "")).strip()
        if not post_id:
            continue
        if post_id in published_ids:
            continue
        if not post.get("image") or not post.get("caption"):
            continue
        return post
    return None


def graph_request(method: str, url: str, token: str, **kwargs) -> dict:
    # Keep the token in the POST body or Authorization header, never in printed URLs.
    headers = kwargs.pop("headers", {})
    headers["Authorization"] = f"Bearer {token}"
    try:
        response = requests.request(
            method, url, headers=headers, timeout=HTTP_TIMEOUT, **kwargs
        )
    except requests.RequestException as exc:
        raise PublishError(f"Network error contacting Meta Graph API: {exc}") from exc

    try:
        payload = response.json()
    except ValueError:
        payload = {"raw_response": response.text[:500]}

    if not response.ok or "error" in payload:
        error = payload.get("error", {})
        message = error.get("message", "Unknown Graph API error")
        code = error.get("code", "unknown")
        raise PublishError(f"Graph API returned HTTP {response.status_code}, code {code}: {message}")
    return payload


def wait_until_ready(container_id: str, graph_base: str, token: str):
    url = f"{graph_base}/{container_id}"
    for attempt in range(6):
        payload = graph_request(
            "GET", url, token,
            params={"fields": "status_code"},
        )
        status = payload.get("status_code")
        if status in (None, "FINISHED", "PUBLISHED"):
            return
        if status == "ERROR":
            raise PublishError(f"Instagram media container {container_id} entered ERROR state.")
        if status == "EXPIRED":
            raise PublishError(f"Instagram media container {container_id} expired before publishing.")
        if status != "IN_PROGRESS":
            raise PublishError(f"Unexpected media container status: {status}")
        if attempt < 5:
            time.sleep(5)
    raise PublishError(f"Media container {container_id} did not finish processing in time.")


def publish_post(post: dict, image_url: str, ig_user_id: str, token: str, graph_base: str) -> str:
    hashtags = []
    for tag in post.get("hashtags", []):
        tag = str(tag).strip().replace(" ", "")
        if tag:
            hashtags.append(tag if tag.startswith("#") else f"#{tag}")
    caption = str(post["caption"]).strip()
    if hashtags:
        caption = caption + "\n\n" + " ".join(hashtags)

    # Instagram's API fetches this public URL; local runner paths will not work.
    parsed = urlparse(image_url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise PublishError("Image URL must be publicly accessible over HTTPS.")

    media_params = {
        "image_url": image_url,
        "caption": caption,
    }
    alt_text = str(post.get("alt_text", "")).strip()
    if alt_text:
        media_params["alt_text"] = alt_text

    container = graph_request(
        "POST",
        f"{graph_base}/{ig_user_id}/media",
        token,
        data=media_params,
    )
    container_id = container.get("id")
    if not container_id:
        raise PublishError("Meta did not return a media container ID.")

    wait_until_ready(container_id, graph_base, token)

    published = graph_request(
        "POST",
        f"{graph_base}/{ig_user_id}/media_publish",
        token,
        data={"creation_id": container_id},
    )
    media_id = published.get("id")
    if not media_id:
        raise PublishError("Meta did not return a published media ID.")
    return str(media_id)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--slot", choices=["08:00", "13:00", "20:00"])
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    try:
        slot = resolve_slot(args)
        queue = load_json(QUEUE_PATH, [])
        state = load_json(STATE_PATH, {"published": []})
        if not isinstance(queue, list):
            raise PublishError("content/queue.json must contain a JSON array.")
        published_records = state.get("published", [])
        published_ids = {
            str(item.get("post_id")) for item in published_records
            if isinstance(item, dict) and item.get("post_id")
        }
        post = choose_post(slot, queue, published_ids)
        if not post:
            print(f"No enabled, unpublished queue item for slot {slot}; skipping safely.")
            return 0

        image_url = build_image_url(os.getenv("PUBLIC_IMAGE_BASE_URL", ""), str(post["image"]))
        print(f"Selected post ID: {post['id']}")
        print(f"Slot (IST): {slot}")
        verify_public_jpeg(image_url)
        print(f"Image URL: {image_url}")
        print(f"Caption preview: {post['caption']}")
        print(f"Hashtags: {' '.join('#' + str(t).lstrip('#').replace(' ', '') for t in post.get('hashtags', []))}")
        if args.dry_run:
            print("DRY RUN: no Instagram API call was made.")
            return 0

        ig_user_id = os.getenv("IG_USER_ID", "").strip()
        token = os.getenv("IG_ACCESS_TOKEN", "").strip()
        version = os.getenv("GRAPH_API_VERSION", "").strip()
        if not ig_user_id or not token or not version:
            raise PublishError("Set IG_USER_ID, IG_ACCESS_TOKEN, and GRAPH_API_VERSION in GitHub Actions secrets.")
        graph_base = f"https://graph.facebook.com/{version}"

        media_id = publish_post(post, image_url, ig_user_id, token, graph_base)
        record = {
            "post_id": str(post["id"]),
            "media_id": media_id,
            "slot": slot,
            "published_at_ist": datetime.now(IST).isoformat(timespec="seconds"),
            "image": str(post["image"]),
        }
        published_records.append(record)
        state["published"] = published_records
        save_json(STATE_PATH, state)
        print(f"Published successfully. Instagram media ID: {media_id}")
        print(f"Saved publication state for post ID: {post['id']}")
        return 0

    except (PublishError, OSError, json.JSONDecodeError, requests.RequestException) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
