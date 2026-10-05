import json

with open("candidates.json", "r") as f:
    candidates = json.load(f)

md = "# Candidate Songs for Ingest\\n\\n"
md += "Here are the final deduped candidate songs pulled from US Billboard Year-End charts, French Rap Hits, and City Pop searches. None of these exist in your current catalog.\\n\\n"
md += "| Title | Artist | Source |\\n"
md += "|-------|--------|--------|\\n"

for c in candidates:
    title = c.get("title", "").replace("|", "\\|")
    artist = c.get("artist", "").replace("|", "\\|")
    source = c.get("source", "").replace("|", "\\|")
    md += f"| {title} | {artist} | {source} |\\n"

with open("/Users/dime/.gemini/antigravity/brain/167ca0d1-d2a8-402b-ada6-d5ebbbe0ef65/candidate_songs.md", "w") as f:
    f.write(md)

# Write metadata
meta = {
    "Summary": "The final deduped candidate list of ~500 songs for the catalog ingest pipeline.",
    "UserFacing": True,
    "RequestFeedback": True
}
with open("/Users/dime/.gemini/antigravity/brain/167ca0d1-d2a8-402b-ada6-d5ebbbe0ef65/candidate_songs.md.metadata.json", "w") as f:
    json.dump(meta, f)
