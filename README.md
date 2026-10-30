# @wanderbyte.exe Instagram Automation

A low-cost, scheduled Instagram photo publisher that publishes each queued image to both the feed and Stories using Python, GitHub Actions, and the Instagram Graph API with Facebook Login.

## What is automated

- Runs Monday through Friday at 08:00, 13:00, and 19:00 IST, and Saturday and Sunday at 10:00 and 18:00 IST.
- Selects the next unpublished queue item for the matching slot.
- Publishes each queued JPEG to the Instagram feed with caption, hashtags, and alt text, then publishes the same JPEG to Stories.
- Records feed and Story media IDs in `state/published.json`; if Story publishing fails after the feed post succeeds, the next run retries the Story without duplicating the feed post.
- Supports manual test runs and manual publishing from the GitHub Actions tab.

## Important limitation: image generation

This repository does **not** automatically generate new images with ChatGPT. Create or edit images in ChatGPT, save them as JPEG files, and add them to `assets/images/`. The publishing workflow is then automatic.

Automated AI image generation requires a separate image-generation API/service. A ChatGPT subscription does not include API credits. If you add an OpenAI API key, API usage is billed separately. This project intentionally does not require an AI API key.

## Folder structure

```text
.
├── .github/workflows/instagram-publish.yml
├── assets/images/              # Public JPEGs served by GitHub Pages
├── content/queue.json          # Captions, hashtags, image names, slot
├── state/published.json        # Automatically updated after publishing
├── publish.py
├── requirements.txt
└── README.md
```

## 1. Create the GitHub repository

1. Create a repository named `wanderbyte-instagram-automation`.
2. For the GitHub Pages approach in this README, make the repository **public**. The images and code will be public, so do not store private photos here.
3. Upload these files to the repository's default branch (usually `main`).
4. Add your own JPEG images under `assets/images/`.
5. Edit `content/queue.json`; image filenames must match exactly.
6. Commit and push the changes.

## 2. Enable GitHub Pages for image hosting

Instagram's publishing API needs an image URL that Meta can fetch publicly. It cannot fetch an image from the GitHub Actions runner's local disk.

1. In the repository, open **Settings → Pages**.
2. Under **Build and deployment**, choose **Deploy from a branch**.
3. Select branch `main` and folder `/(root)`, then save.
4. Wait for Pages to deploy.
5. Set repository variable `PUBLIC_IMAGE_BASE_URL` to the base URL for your repository's Pages site, ending with `/`, for example:
   `https://YOUR_GITHUB_USERNAME.github.io/wanderbyte-instagram-automation/`
6. Confirm an image URL opens in an incognito/private browser, for example:
   `https://YOUR_GITHUB_USERNAME.github.io/wanderbyte-instagram-automation/assets/images/nature-001.jpg`

If you don't want your images and source files public, use a media host with public image URLs instead (for example, a suitable Cloudinary plan) and change `PUBLIC_IMAGE_BASE_URL`/the image workflow accordingly. Check that provider's current free limits.

## 3. Configure Instagram and Meta

This publisher uses the **Instagram Graph API with Facebook Login** for both feed and Story publishing. Stories require an Instagram Business account; Creator accounts are not eligible for Story publishing through this API.

Prerequisites:
- Instagram account is **Business**. Meta's Content Publishing API supports Stories only for Business accounts.
- It is connected to a Facebook Page that you can administer.
- Your Meta app has the Instagram Graph API / Instagram use case configured.
- The app has the required permissions and a valid user access token. Publishing for accounts you don't own/manage may require App Review and Advanced Access.

Typical permissions for this Facebook Login flow:
- `instagram_basic`
- `instagram_content_publish`
- `pages_show_list`
- `pages_read_engagement`

Meta's dashboard and requirements can change. Follow the current official Meta documentation and use the API version available to your app.

### Find the Instagram professional account ID and token

Use Meta's Graph API Explorer or your app's OAuth flow to obtain a user access token with the required permissions. Then query:

`GET https://graph.facebook.com/v{API_VERSION}/me/accounts?fields=id,name,access_token,instagram_business_account`

Find the Facebook Page connected to your Instagram account. Use the `instagram_business_account.id` value as `IG_USER_ID`. Use the connected Page's `access_token` value as `IG_ACCESS_TOKEN` (a Facebook Page access token, not an Instagram Login token). Follow Meta's current token-expiry and long-lived-token instructions; do not assume a short-lived token will keep working indefinitely.

You can also inspect the Page/Instagram account relationship in Meta Business Suite. Never paste your access token into the repository, a source file, or a public issue.

Official documentation:
- Instagram content publishing: https://developers.facebook.com/docs/instagram-platform/content-publishing/
- Instagram API with Facebook Login: https://developers.facebook.com/docs/instagram-platform/instagram-api-with-facebook-login/
- Graph API Explorer: https://developers.facebook.com/tools/explorer/

## 4. Add GitHub secrets and variables

Open **Repository → Settings → Secrets and variables → Actions**.

Add these **Repository secrets**:
- `IG_USER_ID` — the Instagram professional account ID.
- `IG_ACCESS_TOKEN` — the access token with the required permissions.
- `GRAPH_API_VERSION` — the supported version number, such as `v25.0`, if that version is available to your app. Check Meta's current supported version; do not blindly copy the example if it is no longer supported.

Add this **Repository variable**:
- `PUBLIC_IMAGE_BASE_URL` — the public GitHub Pages base URL, ending with `/`.

GitHub Actions secrets are encrypted and can be referenced in workflows. Never print tokens in logs. Rotate a token immediately if it is exposed.

## 5. Add and manage posts

Edit `content/queue.json`. Each item needs:
- `id`: unique ID, never reuse it.
- `slot`: one of `08:00`, `13:00`, `19:00`, `10:00`, or `18:00`.
- `image`: relative path under the repository root.
- `caption`: caption text without hashtags.
- `hashtags`: list of hashtags, with or without `#`.
- `alt_text`: concise description for accessibility.
- `enabled`: set to `true` to make it eligible.

The included queue is a small starter sample, not a full backlog. Add unique queue items and matching JPEGs for every slot you want to fill. A 30-day backlog for this schedule requires about 81–82 posts, depending on which weekdays the month starts and ends with. The script picks the first enabled, unpublished item matching the slot. Keep a backlog of images for each slot. When a slot has no eligible item, the run safely skips instead of posting the wrong content.

Example: use `assets/images/morning-forest.jpg` in the JSON and place that file at exactly that path.

### Create images with ChatGPT

1. Generate an original image in ChatGPT or edit one of your own photos.
2. Save/export it as **JPEG**. For Instagram feed image publishing, use a portrait 4:5 image such as 1080×1350, or a square image.
3. Add it under `assets/images/`.
4. Add its metadata and caption to `content/queue.json`.
5. Commit the changes. The next scheduled slot can publish it.

Be honest about AI-generated scenes; don't caption a generated scene as a real trip you personally took.

## 6. Schedule

The workflow uses UTC cron:
- `30 2 * * 1-5` = weekdays at 08:00 IST
- `30 7 * * 1-5` = weekdays at 13:00 IST
- `30 13 * * 1-5` = weekdays at 19:00 IST
- `30 4 * * 0,6` = weekends at 10:00 IST
- `30 12 * * 0,6` = weekends at 18:00 IST

GitHub scheduled workflows can be delayed during periods of high load and are not a guaranteed real-time scheduler. Scheduled runs execute from the default branch. Keep the workflow on `main` (or update the YAML if your default branch differs).

## 7. Test safely

1. First run **Actions → Instagram photo publisher → Run workflow** with `dry_run = true`. It validates the selected queue item and checks that the image URL is publicly reachable and returns JPEG content, without publishing.
2. Then run manually with `dry_run = false` and a chosen slot to publish one post.
3. Check the Instagram account and the workflow logs.
4. Scheduled runs use `dry_run = false` automatically.

Manual workflow runs use the current IST date only for logs; queue selection is based on the slot and the unpublished queue state.

## 8. Cost and limits

Free in this design:
- GitHub repository (public).
- GitHub Actions within the account's free usage allowance.
- GitHub Pages for a public repository.
- Python and the Instagram Graph API itself, subject to Meta's eligibility, permissions, and limits.
- Manual image generation in ChatGPT within your plan's available usage.

Potentially paid / not included:
- API-based AI image generation or AI caption generation. ChatGPT subscription and API billing are separate.
- Paid image hosting if you do not want public GitHub Pages.
- Additional GitHub Actions usage beyond the plan's allowance.
- Any Meta app review/business requirements that apply to your account; the API is not a guaranteed bypass for account restrictions.

This schedule has about 81–82 posts in a 30-day month. The code does not bypass Instagram's policies or rate limits.

## Troubleshooting

- **OAuthException / permissions error**: regenerate a token with the correct permissions and check app/account setup.
- **Invalid image URL**: open the exact URL in a private browser; confirm it returns a JPEG without login.
- **Unsupported image**: use a valid JPEG file and a supported aspect ratio/size.
- **No eligible queue item**: add an enabled post for that slot with a unique ID.
- **Token expired**: refresh/reissue according to Meta's current token lifecycle and update the GitHub secret.
- **Pages URL 404**: wait for Pages deployment and verify Settings → Pages source.
- **Duplicate concern**: the script stores successful post IDs in `state/published.json`. If a publish succeeds but the runner fails before committing this state, check Instagram before rerunning.

## Safety notes

- Do not store Meta access tokens in code, JSON files, repository variables, or commits.
- Keep repository write access restricted; collaborators with write access can change workflows that use secrets.
- Review captions and images before adding them to the queue.
# wanderbyte-instagram-automation
