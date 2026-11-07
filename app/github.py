"""GitHub public repository API client with a short cache."""
from functools import lru_cache
import requests


@lru_cache(maxsize=1)
def fetch_repositories(username):
    response = requests.get(
        f"https://api.github.com/users/{username}/repos",
        params={"sort": "updated", "direction": "desc", "per_page": 12, "type": "owner"},
        headers={"Accept": "application/vnd.github+json", "User-Agent": "ZKS95-Developer"},
        timeout=8,
    )
    response.raise_for_status()
    repos = response.json()
    return [
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
