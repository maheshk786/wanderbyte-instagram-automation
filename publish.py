
#!/usr/bin/env python3
"""Publish one queued JPEG image using Instagram Login."""

from __future__ import annotations

import argparse
import json
import os
import re
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
HTTP_TIMEOUT = 30

SCHEDULE_TO_SLOT = {
    "30 2 * * 1-5": "08:00",
    "30 7 * * 1-5": "13:00",
    "30 13 * * 1-5": "19:00",
    "30 4 * * 0,6": "10:00",
    "30 12 * * 0,6": "18:00",
}

SLOTS = {"08:00", "13:00", "19:00", "10:00", "18:00"}


class PublishError(RuntimeError):
    pass


def load_json(path: Path, default):
    if not path.exists():
        return default

    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(path.suffix + ".tmp")

    with temporary_path.open("w", encoding="utf-8") as file:
        json.dump(data, file, indent=2, ensure_ascii=False)
        file.write("\n")

    temporary_path.replace(path)


def resolve_slot(args) -> str:
    slot = args.slot

    if not slot:
        scheduled_time = os.getenv("GITHUB_EVENT_SCHEDULE", "")
        slot = SCHEDULE_TO_SLOT.get(scheduled_time, "")

    if slot not in SLOTS:
        raise PublishError(
            "Could not determine posting slot. "
            "Choose 08:00, 13:00, or 19:00 IST on weekdays; 10:00 or 18:00 IST on weekends."
        )

    return slot


def build_image_url(base_url: str, image_path: str) -> str:
    if not base_url:
        raise PublishError(
            "PUBLIC_IMAGE_BASE_URL is missing. Configure it in "
            "GitHub repository Variables."
        )

    parsed = urlparse(base_url)

    if parsed.scheme != "https" or not parsed.netloc:
        raise PublishError(
            "PUBLIC_IMAGE_BASE_URL must be a public HTTPS URL."
        )

    relative_path = image_path.strip().lstrip("/")

    if not relative_path or ".." in Path(relative_path).parts:
        raise PublishError(f"Invalid image path: {image_path}")

    if not relative_path.lower().endswith((".jpg", ".jpeg")):
        raise PublishError(
            f"Instagram image must be a JPEG: {image_path}"
        )

    return base_url.rstrip("/") + "/" + relative_path


def verify_public_jpeg(image_url: str):
    """Verify the image is publicly reachable and served as a JPEG."""

    try:
        response = requests.get(
            image_url,
            allow_redirects=True,
            stream=True,
            timeout=HTTP_TIMEOUT,
        )

        try:
            if not response.ok:
                raise PublishError(
                    "Public image check failed with HTTP "
                    f"{response.status_code}. Check GitHub Pages and "
                    f"the image path: {image_url}"
                )

            content_type = (
                response.headers.get("Content-Type", "")
                .split(";", 1)[0]
                .strip()
                .lower()
            )

            if content_type not in {"image/jpeg", "image/jpg"}:
                raise PublishError(
                    "The image URL must serve image/jpeg. "
                    f"Server returned: {content_type or 'unknown'}"
                )

        finally:
            response.close()

    except requests.RequestException as exc:
        raise PublishError(
            f"Could not access the public image URL: {exc}"
        ) from exc


def choose_post(slot: str, queue: list, published_ids: set):
    for post in queue:
        if not isinstance(post, dict):
            continue

        if post.get("enabled", True) is not True:
            continue

        if post.get("slot") != slot:
            continue

        post_id = str(post.get("id", "")).strip()

        if not post_id or post_id in published_ids:
            continue

        if not post.get("image") or not post.get("caption"):
            continue

        return post

    return None


def build_caption(post: dict) -> str:
    caption = str(post.get("caption", "")).strip()

    hashtags = []
    for tag in post.get("hashtags", []):
        tag = str(tag).strip().replace(" ", "")
        if tag:
            hashtags.append(tag if tag.startswith("#") else f"#{tag}")

    if hashtags:
        caption += "\n\n" + " ".join(hashtags)

    if len(caption) > 2200:
        raise PublishError(
            f"Caption is {len(caption)} characters; Instagram captions "
            "must be no longer than 2,200 characters."
        )

    return caption


def graph_request(
    method: str,
    url: str,
    token: str,
    **kwargs,
) -> dict:
    """Call Meta without placing the access token in the URL or logs."""

    headers = kwargs.pop("headers", {})
    headers["Authorization"] = f"Bearer {token}"

    try:
        response = requests.request(
            method,
            url,
            headers=headers,
            timeout=HTTP_TIMEOUT,
            **kwargs,
        )
    except requests.RequestException as exc:
        # Do not print request headers or credentials.
        raise PublishError(
            f"Network error contacting Instagram: {type(exc).__name__}"
        ) from exc

    try:
        try:
            payload = response.json()
        except ValueError:
            payload = {}

        if not response.ok or "error" in payload:
            error = payload.get("error", {})
            message = error.get("message", "Unknown API error")
            code = error.get("code", "unknown")

            raise PublishError(
                f"Instagram API HTTP {response.status_code}, "
                f"code {code}: {message}"
            )

        return payload

    finally:
        response.close()


def wait_until_ready(
    container_id: str,
    graph_base: str,
    token: str,
):
    """Wait for Instagram to finish processing the media container."""

    status_url = f"{graph_base}/{container_id}"

    for attempt in range(12):
        payload = graph_request(
            "GET",
            status_url,
            token,
            params={"fields": "status_code"},
        )

        status = payload.get("status_code")

        if status in {"FINISHED", "PUBLISHED"}:
            return

        if status == "ERROR":
            raise PublishError(
                "Instagram reported an error processing the image."
            )

        if status == "EXPIRED":
            raise PublishError(
                "The Instagram media container expired."
            )

        if status not in {"IN_PROGRESS", None}:
            raise PublishError(
                f"Unexpected Instagram container status: {status}"
            )

        if attempt < 11:
            time.sleep(5)

    raise PublishError(
        "Instagram did not finish processing the image in time."
    )


def publish_post(
    post: dict,
    image_url: str,
    ig_user_id: str,
    token: str,
    graph_base: str,
) -> str:
    caption = build_caption(post)

    media_params = {
        "image_url": image_url,
        "caption": caption,
    }

    alt_text = str(post.get("alt_text", "")).strip()
    if alt_text:
        media_params["alt_text"] = alt_text

    # Step 1: Create an Instagram media container.
    container = graph_request(
        "POST",
        f"{graph_base}/{ig_user_id}/media",
        token,
        data=media_params,
    )

    container_id = container.get("id")
    if not container_id:
        raise PublishError(
            "Instagram did not return a media container ID."
        )

    print("Instagram media container created.")

    # Step 2: Wait until Instagram finishes processing the image.
    wait_until_ready(container_id, graph_base, token)

    # Step 3: Publish the container to the Instagram account.
    published = graph_request(
        "POST",
        f"{graph_base}/{ig_user_id}/media_publish",
        token,
        data={"creation_id": container_id},
    )

    media_id = published.get("id")
    if not media_id:
        raise PublishError(
            "Instagram did not return a published media ID."
        )

    return str(media_id)



def move_image_to_processed(image_path: str, post_id: str) -> str:
    """Move a published JPEG into assets/images/Processed."""
    source = (ROOT / image_path).resolve()
    images_dir = (ROOT / "assets" / "images").resolve()
    try:
        source.relative_to(images_dir)
    except ValueError as exc:
        raise PublishError("Only files under assets/images can be moved to Processed.") from exc

    if source.parent == (images_dir / "Processed"):
        return source.relative_to(ROOT).as_posix()

    processed_dir = images_dir / "Processed"
    processed_dir.mkdir(parents=True, exist_ok=True)
    destination = processed_dir / source.name

    if destination.exists() and source.exists():
        safe_post_id = re.sub(r"[^A-Za-z0-9._-]", "_", post_id)
        destination = processed_dir / f"{source.stem}-{safe_post_id}{source.suffix}"

    if destination.exists():
        if source.exists():
            raise PublishError(f"Processed image destination already exists: {destination.name}")
        return destination.relative_to(ROOT).as_posix()

    if not source.exists():
        raise PublishError(f"Published image file is missing: {image_path}")

    source.replace(destination)
    return destination.relative_to(ROOT).as_posix()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Publish one queued Instagram photo to the feed."
    )
    parser.add_argument(
        "--slot",
        choices=sorted(SLOTS),
        help="Posting slot in India Standard Time.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate the queued post without publishing.",
    )
    args = parser.parse_args()

    try:
        slot = resolve_slot(args)

        queue = load_json(QUEUE_PATH, [])
        state = load_json(STATE_PATH, {"published": []})

        if not isinstance(queue, list):
            raise PublishError(
                "content/queue.json must contain a JSON array."
            )

        if not isinstance(state, dict):
            raise PublishError("state/published.json must be a JSON object.")

        published_records = state.setdefault("published", [])

        # Older feed-only records are complete; don't retroactively move their images.
        for item in published_records:
            if isinstance(item, dict) and item.get("media_id") and "image_status" not in item:
                item["image_status"] = "legacy_not_moved"

        published_ids = {
            str(item.get("post_id"))
            for item in published_records
            if isinstance(item, dict)
            and item.get("post_id")
            and item.get("image_status") in {"moved", "legacy_not_moved"}
        }

        post = choose_post(slot, queue, published_ids)

        if not post:
            print(
                f"No enabled, unpublished post for {slot} IST. Skipping."
            )
            return 0

        post_id = str(post["id"])
        record = next(
            (item for item in published_records
             if isinstance(item, dict) and str(item.get("post_id")) == post_id),
            None,
        )
        image_url = build_image_url(
            os.getenv("PUBLIC_IMAGE_BASE_URL", ""),
            str(post["image"]),
        )

        print(f"Selected post: {post_id}")
        print(f"Posting slot: {slot} IST")
        print(f"Image URL: {image_url}")
        print(f"Caption preview: {build_caption(post)}")

        if not record or not record.get("media_id"):
            verify_public_jpeg(image_url)
            print("Public JPEG URL verified.")

        if args.dry_run:
            print("DRY RUN successful. No Instagram post was published.")
            return 0

        ig_user_id = os.getenv("IG_USER_ID", "").strip()
        token = os.getenv("IG_ACCESS_TOKEN", "").strip()
        version = os.getenv("GRAPH_API_VERSION", "").strip()

        if not ig_user_id or not token or not version:
            raise PublishError(
                "Missing IG_USER_ID, IG_ACCESS_TOKEN, or GRAPH_API_VERSION in GitHub Actions secrets."
            )

        if not re.fullmatch(r"v\d+\.0", version):
            raise PublishError(
                "GRAPH_API_VERSION must look like v26.0. Use a currently supported version from Meta."
            )

        # Feed publishing uses Instagram Graph API with Facebook Login.
        graph_base = f"https://graph.facebook.com/{version}"
        if record and record.get("media_id"):
            print(f"Feed post already published for {post_id}; retrying image processing only.")
        else:
            media_id = publish_post(
                post=post,
                image_url=image_url,
                ig_user_id=ig_user_id,
                token=token,
                graph_base=graph_base,
            )
            record = {
                "post_id": post_id,
                "media_id": media_id,
                "slot": slot,
                "published_at_ist": datetime.now(IST).isoformat(timespec="seconds"),
                "image": str(post["image"]),
                "image_status": "pending",
            }
            published_records.append(record)
            state["published"] = published_records
            save_json(STATE_PATH, state)
            print(f"Feed post published. Media ID: {media_id}")
            print("Saved publication state before moving the image.")

        if record.get("image_status") != "moved":
            processed_image = move_image_to_processed(str(post["image"]), post_id)
            record["processed_image"] = processed_image
            record["image_status"] = "moved"
            state["published"] = published_records
            save_json(STATE_PATH, state)
            print(f"Moved published image to {processed_image}.")

        print(f"Recorded feed publication and image processing state for {post_id}.")
        return 0


    except (
        PublishError,
        OSError,
        ValueError,
        requests.RequestException,
    ) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
