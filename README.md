# MP3 Tagger

Small self-hosted web app (Flask + mutagen) for editing ID3 tags on MP3 files:
all common fields, custom TXXX fields, lyrics, comments, album cover, and bulk edit.

## Deploy
1. Push this repo to GitHub (the Actions workflow builds `ghcr.io/<you>/mp3-tagger:latest`).
2. In GitHub: Packages -> mp3-tagger -> Package settings -> set visibility to Public (or add registry credentials in TrueNAS).
3. In TrueNAS: Apps -> Discover Apps -> Custom App -> Install via YAML, paste `docker-compose.yml` after editing the image name, SMB address and credentials.
4. Open `http://<truenas-ip>:8080`.

Do not commit real SMB credentials to a public repo. Paste them in TrueNAS only, or keep the repo private.
There is no login, so keep it on your LAN.
