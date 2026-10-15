"""The content pipeline: source in, a promotion kit out (quote posts, thumbnails, carousels, reel cards).

ContentDoc (`manifest`) is to content what EditDoc is to video: versioned, validated, never overwritten. Templates live in a
registry (`registry`), words come from sources and are checked VERBATIM (`sources`), renderers turn items into hashed files
(`render_motionkit`), a human reviews a contact sheet (`review`), and approved items enter the hash-bound publish queue
(`queueing`). Additive only: nothing here is imported by the video pipeline.
"""
