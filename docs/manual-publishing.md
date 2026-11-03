# Export a post and publish it yourself

Use the visual planner to arrange the feed and review each post. When you want to finish in Instagram yourself, export a manual publishing pack. It contains the actual full-resolution media files, not a screenshot or the cropped grid thumbnails. AVE does not open Instagram, upload anything, or mark a downloaded post as published.

## From the CLI

```sh
ave export-post /absolute/path/to/studio p_POST_ID --variant v_VARIANT_ID --check
ave export-post /absolute/path/to/studio p_POST_ID --variant v_VARIANT_ID --expected-rev 7
```

The first command is a read-only readiness check. The second creates a new immutable folder and ZIP under the studio's `work/manual_exports/`. Use `--output /absolute/path/to/export-folder` to choose another parent folder. Every new export gets a unique ID; earlier exports are never overwritten.

The default **reviewed** mode requires the current post, media order and exact text to match this studio's signed human approval, with a covered license closure and no blocking feedback. A live account, social login and automatic-sender permission are not required. Exporting does not grant any of them. Changed or expired approvals need review again. Raising an approval lifetime after review cannot revive it: reviewed exports compare current publish/studio lifetimes with the policy signed into the approval. The exact files currently bound to the variant are copied without resizing, recompression or grid crops. If the pipeline prepared a platform-compatible JPEG, that reviewed JPEG is the export; the original source asset remains in the library. An unconverted source post without variants can export that source as a draft.

For planning or design review before you have a connected account or reviewed queue item:

```sh
ave export-post /absolute/path/to/studio p_POST_ID --draft
```

A post without variants can export as a draft with Instagram-style caption composition. Draft packs clearly say **DRAFT — NOT REVIEWED** and include licensing/readiness warnings. They are not permission to publish. Draft mode cannot bypass missing media, changed registered bytes, unsafe paths or file limits.

## What is in the pack

- Ordered media filenames beginning `01-`, `02-`, and so on, preserving the variant's carousel slot order.
- `caption.txt`: the composed caption, link, required credits and hashtags.
- `first-comment.txt`: the first comment, when applicable.
- `alt-text.txt`: alt text for each image in posting order.
- `manifest.json`: dimensions, exact SHA-256 hashes, review mode, source revision and any warnings. It contains no credentials or transferable publishing approval.
- `index.html`: an offline page with original-file links and selectable copy, sized for a phone.

Transfer the ZIP through your chosen channel, such as AirDrop or a deliberately shared folder. On your phone, extract it in Files. The offline page can help you find each file and copy the caption; iOS may require **Files → Share → Save Image/Save Video**. Then open Instagram yourself, select media in its numbered order, paste the caption and add alt text. Downloading a ZIP does not automatically put its images into Photos or create an Instagram post.

The offline page has no scripts, remote assets, tracking or network connection. It works separately from the desktop server; it is not an iOS companion app. Native device sharing, Photos integration and a live mobile companion remain separate roadmap work.

## Limits and safety

Packs allow up to 20 media files, 512 MiB per file and 1 GiB across both the ordered media payload (including repeated carousel assets) and the unique provenance closure. Source metadata is bounded. Only registered library media is read; raw recordings, arbitrary paths and symlinks are refused. Output guards inspect bounded ancestor episode/studio markers too, so a caller cannot redirect a pack into a sibling episode’s flat or numbered raw folders. The copied bytes are hashed again, and the revision and reviewed state are checked after copying. The completed ZIP becomes visible by atomic rename under a regular-file lock; this works without hardlinks or symlinks on ExFAT. A failed copy does not expose a partial ZIP. If a process is killed during final folder assembly, its folder retains an `.incomplete` marker and no completed ZIP; retrying creates a fresh package while preserving that evidence. The queue, account settings, approvals, scheduling and publishing history are unchanged.

There is no claim that manually posting improves algorithmic reach. This workflow exists so the creator controls the final step and can use the native platform's tools when desired.
