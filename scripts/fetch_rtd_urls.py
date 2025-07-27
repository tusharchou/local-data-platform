import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse


def fetch_all_rtd_urls(base_url):
    visited = set()
    to_visit = [base_url]
    result = []

    while to_visit:
        url = to_visit.pop()
        if url in visited:
            continue
        visited.add(url)
        try:
            resp = requests.get(url)
            resp.raise_for_status()
        except Exception as e:
            print(f"Failed to fetch {url}: {e}")
            continue

        soup = BeautifulSoup(resp.text, "html.parser")
        result.append(url)
        for link in soup.find_all("a", href=True):
            href = link["href"]
            # Only follow internal links
            if href.startswith("http"):
                if not href.startswith(base_url):
                    continue
            elif href.startswith("/"):
                href = urljoin(base_url, href)
            else:
                href = urljoin(url, href)
            # Only crawl pages within the docs site
            if urlparse(href).netloc == urlparse(base_url).netloc and href not in visited:
                to_visit.append(href)

    return sorted(result)


def write_urls_to_wiki(urls, output_path):
    with open(output_path, "w") as f:
        f.write("# Active Hosted Docs URLs\n\n")
        for url in urls:
            f.write(f"- [{url}]({url})\n")


def main():
    # Updated to your actual Read the Docs URL and branch
    BASE_URL = "https://local-data-platform.readthedocs.io/en/docs-sidebar-recipes-from-fix-readthedocs/"
    urls = fetch_all_rtd_urls(BASE_URL)
    write_urls_to_wiki(urls, "docs/wiki/ACTIVE_DOCS_URLS.md")
    print(f"Found {len(urls)} URLs. Output written to docs/wiki/ACTIVE_DOCS_URLS.md")


if __name__ == "__main__":
    main()
