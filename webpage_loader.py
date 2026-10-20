"""Load only configured public URLs; no crawling or authentication bypass."""
import re
import time
from urllib.parse import urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup
from langchain_core.documents import Document

USER_AGENT = "EnterpriseKnowledgeAssistant/1.0 (personal portfolio document loader)"
ROBOT_AGENT = "EnterpriseKnowledgeAssistant"


def normalize_url(url):
    if not isinstance(url, str):
        raise ValueError("URL must be a string.")
    url = url.strip()
    parts = urlsplit(url)
    if (parts.scheme not in {"http", "https"} or not parts.hostname
            or parts.username or parts.password or re.search(r"\s", url)):
        raise ValueError("Use an http(s) URL with a hostname and no credentials.")
    # Accessing .port also rejects malformed port values.
    port = parts.port
    host = parts.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    if port and not ((parts.scheme == "https" and port == 443)
                     or (parts.scheme == "http" and port == 80)):
        host += f":{port}"
    return urlunsplit((parts.scheme, host, parts.path or "/", parts.query, ""))


def extract_webpage(html, url):
    """Return section Documents, retaining headings and list/paragraph boundaries."""
    soup = BeautifulSoup(html, "html.parser")
    page_title = soup.title.get_text(" ", strip=True) if soup.title else url
    for node in soup.select("script, style, noscript, nav, footer, aside, form, iframe, "
                            "[hidden], [aria-hidden='true'], [role='navigation'], "
                            "[role='banner'], [role='contentinfo'], [role='complementary']"):
        node.decompose()
    # Remove recognizable site chrome, not every element containing 'menu'.
    chrome = re.compile(r"(?:^|[-_\s])(sidebar|breadcrumb|cookie|toc|" 
                        r"table-of-contents|site-header|site-footer|site-nav)(?:$|[-_\s])", re.I)
    for node in list(soup.find_all(True)):
        if node.attrs is None:
            continue
        identity = " ".join([node.get("id", ""), *node.get("class", [])])
        if chrome.search(identity):
            node.decompose()
    # A site header outside an article is chrome; an article header may hold its title.
    for node in list(soup.find_all("header")):
        if not node.find_parent(["article", "main"]):
            node.decompose()
    root = soup.select_one(".td-content")  # GitLab's article body, when available.
    if root is None:
        root = soup.find("article") or soup.find("main") or soup.find(attrs={"role": "main"})
    if root is None:
        # Structural fallback: choose the richest content-bearing container.
        candidates = soup.find_all(["section", "div"])
        root = max(candidates, key=lambda node: sum(
            len(item.get_text(" ", strip=True)) for item in node.find_all(["p", "li"])
        ), default=None)
        if root is None or not root.find(["p", "li"]):
            root = soup.body or soup
    h1 = root.find("h1")
    title = h1.get_text(" ", strip=True) if h1 else page_title
    if re.search(r"sign[ -]?in|log[ -]?in|access denied|just a moment", title, re.I):
        raise ValueError("Page appears to be an access/login challenge; not ingested.")
    sections, lines, headings = [], [], {}
    body_found = False

    def finish_section():
        if not lines:
            return
        metadata = {"source": url, "title": title, "document_type": "webpage"}
        if headings:
            metadata["section"] = " > ".join(headings[level] for level in sorted(headings))
        sections.append(Document(page_content="\n\n".join(lines), metadata=metadata))
        lines.clear()

    for node in root.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "pre", "tr"]):
        # A paragraph in a list item is emitted as part of that item, once.
        if node.find_parent(["p", "li", "pre", "tr"]) is not None:
            continue
        text = " ".join(node.get_text(" ", strip=True).split())
        if not text:
            continue
        if node.name.startswith("h"):
            finish_section()
            level = int(node.name[1])
            for old in list(headings):
                if old >= level:
                    del headings[old]
            headings[level] = text
        else:
            body_found = True
        if node.name == "li":
            # Remove nested lists from this item's text; emit their items separately.
            own = BeautifulSoup(str(node), "html.parser").find("li")
            for nested in own.find_all(["ul", "ol"]):
                nested.decompose()
            text = " ".join(own.get_text(" ", strip=True).split())
            text = "- " + text
            for nested in node.find_all("li"):
                text += "\n  - " + " ".join(nested.get_text(" ", strip=True).split())
        lines.append(text)
    finish_section()
    if not sections:
        # For simple pages without semantic block tags, retain cleaned visible text.
        text = "\n\n".join(" ".join(line.split()) for line in root.get_text("\n").splitlines()
                           if line.strip())
        if text:
            body_found = text != title and len(text) >= 10
            sections = [Document(page_content=text, metadata={
                "source": url, "title": title, "document_type": "webpage"})]
    if not sections or not body_found:
        raise ValueError("No meaningful article text extracted.")
    return sections


def load_webpages(urls, timeout=20, interval=1.0, session=None):
    """Return (documents, errors). Failed sources do not produce replacement chunks."""
    if timeout <= 0 or interval < 0:
        raise ValueError("timeout must be positive and interval must not be negative.")
    if isinstance(urls, str):
        urls = [urls]
    documents, errors, seen, robots, last_request = [], {}, set(), {}, {}
    own_session = session is None
    session = session or requests.Session()

    def download(url, delay):
        origin = "{0.scheme}://{0.netloc}".format(urlsplit(url))
        if origin in last_request:
            time.sleep(max(0, delay - (time.monotonic() - last_request[origin])))
        try:
            return session.get(url, headers={"User-Agent": USER_AGENT},
                               timeout=timeout, allow_redirects=False)
        finally:
            last_request[origin] = time.monotonic()

    try:
        for original in urls:
            try:
                url = normalize_url(original)
                if url in seen:
                    continue
                seen.add(url)
                origin = "{0.scheme}://{0.netloc}".format(urlsplit(url))
                if origin not in robots:
                    response = download(origin + "/robots.txt", interval)
                    if response.status_code == 404:
                        rules = []  # Missing robots.txt means no published robot rules.
                    elif response.status_code == 200:
                        if "<html" in response.text.lower():
                            raise ValueError("robots.txt returned HTML; access rules not verified.")
                        rules = response.text.splitlines()
                    else:
                        raise ValueError(f"Cannot verify robots.txt (HTTP {response.status_code}).")
                    parser = RobotFileParser()
                    parser.parse(rules)
                    robots[origin] = parser
                parser = robots[origin]
                if not parser.can_fetch(ROBOT_AGENT, url):
                    raise ValueError("Blocked by robots.txt; webpage not requested.")
                delay = max(interval, parser.crawl_delay(ROBOT_AGENT) or 0)
                rate = parser.request_rate(ROBOT_AGENT)
                if rate:
                    delay = max(delay, rate.seconds / rate.requests)
                response = download(url, delay)
                if 300 <= response.status_code < 400:
                    raise ValueError("URL redirects. Configure its public destination explicitly; "
                                     "redirects are not followed.")
                response.raise_for_status()
                content_type = response.headers.get("Content-Type", "").lower()
                if not any(kind in content_type for kind in ("text/html", "application/xhtml+xml")):
                    raise ValueError("URL did not return HTML.")
                documents.extend(extract_webpage(response.text, url))
            except (ValueError, requests.RequestException) as error:
                errors[str(original)] = str(error)
    finally:
        if own_session:
            session.close()
    return documents, errors
