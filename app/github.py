"""GitHub public repository API client with a short cache."""
import time
import requests

_cache = {}
_cache_ttl = 60

def fetch_repositories(username, endpoint=None):
    cache_key = (username, endpoint or "")
    cached = _cache.get(cache_key)
    if cached and time.monotonic() - cached[0] < _cache_ttl:
        return cached[1]
    response = requests.get(
        endpoint or f"https://api.github.com/users/{username}/repos",
        params={"sort": "updated", "direction": "desc", "per_page": 12, "type": "owner"},
        headers={"Accept": "application/vnd.github+json", "User-Agent": "ZKS95-Developer"},
        timeout=8,
    )
    response.raise_for_status()
    repos = response.json()
    repositories = [
        {
            "name": repo["name"],
            "description": repo.get("description") or "Открытый проект ZKS95 Developer",
            "html_url": repo["html_url"],
            "language": repo.get("language"),
            "stargazers_count": repo.get("stargazers_count", 0),
            "forks_count": repo.get("forks_count", 0),
            "updated_at": repo.get("updated_at", "")[:10],
        }
        for repo in repos
    ]
    _cache[cache_key] = (time.monotonic(), repositories)
    return repositories
