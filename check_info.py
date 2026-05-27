import yt_dlp, json
ydl = yt_dlp.YoutubeDL({"quiet": True})
info = ydl.extract_info("https://www.youtube.com/watch?v=dQw4w9WgXcQ", download=False)
keys = ["heatmap", "chapters", "chapter", "segments", "most_replayed", "replayed"]
for k in keys:
    if k in info:
        print(f"{k}: {json.dumps(info[k], indent=2)[:500]}")
    else:
        print(f"{k}: not found")
print(f"duration: {info.get('duration')}")
print(f"title: {info.get('title')}")
