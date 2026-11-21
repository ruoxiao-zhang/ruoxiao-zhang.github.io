#!/usr/bin/env python3
"""Build a daily, source-attributed Summer 2027 US/Canada internship feed.

Python standard library only. Data comes from the public SimplifyJobs list and
selected employers' public Ashby job boards. No credential, scraping login, or AI API.
"""
import hashlib
import html
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import sys
from datetime import datetime, timedelta, timezone
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / 'public' / 'intern-radar' / 'jobs.json'
SIMPLIFY_URL = 'https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/dev/README.md'
ASHBY_BOARDS = ['steel', 'cresta', 'sierra', 'decagon', 'cohere', 'perplexity', 'replit', 'ramp', 'mercor', 'modal', 'harvey', 'glean', 'langchain', 'vanta', 'fireworks-ai', 'quadrillion']
STATES = set('AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI DC'.split())
PROVINCES = set('AB BC MB NB NL NS NT NU ON PE QC SK YT'.split())
CANADA_CITIES = ('toronto', 'waterloo', 'kitchener', 'ottawa', 'montreal', 'montréal', 'vancouver', 'calgary', 'edmonton', 'markham', 'mississauga', 'richmond hill', 'burnaby', 'quebec', 'halifax', 'winnipeg', 'victoria', 'canada')
US_CITIES = ('new york', 'san francisco', 'seattle', 'boston', 'chicago', 'austin', 'palo alto', 'menlo park', 'mountain view', 'sunnyvale', 'san jose', 'washington', 'united states', 'usa', 'u.s.', 'los angeles', 'atlanta', 'pittsburgh', 'redmond', 'bellevue', 'remote - us')
BLOCKED = re.compile(r'\b(senior|staff|principal|director|head of|manager|full.time|new grad|graduate program|phd|ph\.d\.|postdoc|doctorate)\b', re.I)
RELEVANT = re.compile(r'engineer|developer|software|computer|machine learning|\bml\b|\bai\b|agent|data|quant|research|science|analytics|product|optimization|simulation|algorithm|technical', re.I)


def fetch(url):
    req = Request(url, headers={'User-Agent': 'RuoxiaoInternRadar/1.0 (+https://github.com/ruoxiao-zhang/ruoxiao-zhang.github.io)', 'Accept': 'application/json, text/plain, */*'})
    with urlopen(req, timeout=28) as res:
        return res.read().decode('utf-8')


class JobTable(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows = []
        self.row = None
        self.cell = None
        self.depth = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == 'tr':
            self.row = []
        elif tag == 'td' and self.row is not None:
            self.cell = {'text': [], 'links': []}
            self.depth = 1
        elif tag == 'a' and self.cell is not None:
            if a.get('href', '').startswith('http'):
                self.cell['links'].append(a['href'])
        elif tag == 'br' and self.cell is not None:
            self.cell['text'].append(' / ')
        elif tag == 'td' and self.cell is not None:
            self.depth += 1

    def handle_data(self, data):
        if self.cell is not None:
            self.cell['text'].append(data)

    def handle_endtag(self, tag):
        if tag == 'td' and self.cell is not None:
            self.depth -= 1
            if self.depth == 0:
                self.cell['text'] = re.sub(r'\s+', ' ', ''.join(self.cell['text'])).strip()
                self.row.append(self.cell)
                self.cell = None
        elif tag == 'tr' and self.row is not None:
            if self.row:
                self.rows.append(self.row)
            self.row = None


def country(location):
    lower = location.lower()
    tokens = set(re.findall(r'\b[A-Z]{2}\b', location))
    ca = bool(tokens & PROVINCES or any(c in lower for c in CANADA_CITIES))
    us = bool(tokens & STATES or any(c in lower for c in US_CITIES))
    if ca and not us:
        return 'Canada'
    if us and not ca:
        return 'United States'
    if ca and us:
        return 'US / Canada'
    return ''


def classify(title, section=''):
    s = (title + ' ' + section).lower()
    t = title.lower()
    if re.search(r'\bquant|trading|option pricing', t):
        return 'Quant & Finance'
    if re.search(r'agent|llm|generative ai|genai|applied ai|\bai\b|machine learning|\bml\b|deep learning|research scientist|ai/ml', t):
        return 'AI & ML'
    if re.search(r'data|analytics|statistic|business intelligence', t):
        return 'Data & Analytics'
    if re.search(r'product manager|product management|technical product', t):
        return 'Product'
    if re.search(r'software|developer|frontend|front.end|backend|back.end|full.stack|devops|platform|infrastructure|cloud|sre|automation', t):
        return 'Software'
    return 'Other Tech'


def score(title, location, company, age, flags):
    text = (title + ' ' + company).lower()
    s = 52
    why = []
    for pat, points, label in [
        (r'agent|agentic', 19, 'Agent workflows'),
        (r'machine learning|\bml\b|\bai\b|llm|genai|applied ai', 15, 'Applied AI'),
        (r'data science|data analyst|data engineering|analytics', 11, 'Math & data'),
        (r'quant|research|optimization|simulation', 10, 'Math & research'),
        (r'product engineer|product management', 7, 'Product building'),
        (r'automation|testing|developer tools|platform|infrastructure', 8, 'Engineering automation')]:
        if re.search(pat, text):
            s += points
            why.append(label)
    if country(location) == 'Canada':
        s += 8
        why.append('Canada co-op friendly')
    if age is not None and age <= 7:
        s += 7
        why.append('New this week')
    if 'no sponsorship' in flags:
        s -= 16
    if 'US citizenship' in flags or 'graduate degree' in flags:
        s -= 35
    return max(5, min(99, s)), why[:3]


def key(company, title, link):
    return hashlib.sha1((company.lower() + '|' + title.lower() + '|' + re.sub(r'[?].*', '', link)).encode()).hexdigest()[:16]


def parse_simplify(markdown):
    jobs = []
    sections = re.split(r'(?=^## .+$)', markdown, flags=re.M)
    for section in sections:
        head = section.splitlines()[0] if section.splitlines() else ''
        if not any(x in head.lower() for x in ('engineering', 'data science', 'quant', 'product')):
            continue
        prev_company = ''
        for table in re.findall(r'<table\b.*?</table>', section, re.I | re.S):
            p = JobTable()
            p.feed(table)
            for row in p.rows:
                if len(row) < 4:
                    continue
                if len(row) == 4 and prev_company:
                    company, role, loc, app = prev_company, row[0], row[1], row[2]
                    age_cell = row[3]
                else:
                    if len(row) < 5:
                        continue
                    company, role, loc, app, age_cell = row[:5]
                    company = company['text']
                    if company and company not in ('↳', '—'):
                        prev_company = company
                    else:
                        company = prev_company
                title = role['text']
                location = loc['text']
                if not company or not title or not RELEVANT.search(title) or BLOCKED.search(title):
                    continue
                region = country(location)
                if not region:
                    continue
                links = [x for x in app['links'] if not ('simplify.jobs' in x or 'i.imgur' in x)]
                if not links:
                    continue
                url = links[0]
                flags = []
                if '🛂' in title:
                    flags.append('no sponsorship')
                if '🇺🇸' in title:
                    flags.append('US citizenship')
                if '🎓' in title:
                    flags.append('graduate degree')
                if '🔒' in title:
                    continue
                title = re.sub(r'[🛂🇺🇸🎓🔥🔒]', '', title).replace('️', '').strip()
                age_match = re.search(r'\b(\d+)d\b', age_cell['text'])
                age = int(age_match.group(1)) if age_match else None
                value, why = score(title, location, company, age, flags)
                jobs.append({'id': key(company, title, url), 'company': company,
                             'title': title, 'location': location, 'country': region,
                             'category': classify(title, head), 'url': url, 'source': 'Simplify / Pitt CSC',
                             'source_type': 'community list', 'source_url': 'https://github.com/SimplifyJobs/Summer2027-Internships',
                             'score': value, 'why': why, 'flags': flags, 'age_days': age,
                             'term': 'Summer 2027 (community-listed)', 'status': 'community-listed'})
    return jobs


def parse_ashby(board, data):
    jobs = []
    for entry in data.get('jobs', []):
        if not entry.get('isListed', True):
            continue
        title = entry.get('title', '')
        if not re.search(r'intern|co.op|student', title, re.I) or BLOCKED.search(title):
            continue
        if not re.search(r'2027|summer\s*[’\-]?\s*27', title, re.I):
            continue  # Ashby does not guarantee a summer term when the title is generic.
        location = entry.get('location', '')
        if isinstance(location, dict):
            location = location.get('name', '')
        region = country(str(location))
        if not region:
            continue
        url = entry.get('jobUrl') or entry.get('applyUrl') or ''
        if not url.startswith('https://'):
            continue
        company = board.replace('-', ' ').title()
        title = str(title)
        value, why = score(title, str(location), company, None, [])
        jobs.append({'id': key(company, title, url), 'company': company,
                     'title': title, 'location': str(location), 'country': region,
                     'category': classify(title), 'url': url, 'source': company + ' (Ashby)',
                     'source_type': 'employer job board', 'source_url': f'https://jobs.ashbyhq.com/{board}',
                     'score': value, 'why': why, 'flags': [], 'age_days': None,
                     'term': 'Summer 2027 (title)', 'status': 'employer-listed'})
    return jobs


def main():
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    sources = []
    try:
        base = parse_simplify(fetch(SIMPLIFY_URL))
        sources.append({'name': 'Simplify / Pitt CSC', 'ok': True, 'count': len(base), 'url': SIMPLIFY_URL})
        if len(base) < 10:
            raise RuntimeError(f'Simplify parser returned only {len(base)} positions')
    except Exception as e:
        print(f'FATAL: Cannot refresh primary feed: {e}', file=sys.stderr)
        sys.exit(1)  # Keep previous JSON and its honest timestamp; no stale overwrite.
    all_jobs = base
    for board in ASHBY_BOARDS:
        url = f'https://api.ashbyhq.com/posting-api/job-board/{board}'
        try:
            items = parse_ashby(board, json.loads(fetch(url)))
            all_jobs.extend(items)
            sources.append({'name': 'Ashby / ' + board, 'ok': True, 'count': len(items)})
        except Exception as e:
            sources.append({'name': 'Ashby / ' + board, 'ok': False, 'error': str(e)[:110]})
    dedup = {}
    for job in all_jobs:
        dedup.setdefault(job['id'], job)
    # Prefer employer evidence when both sources list same job, and retain prior first seen.
    previous = {}
    if OUTPUT.exists():
        try:
            previous = {j['id']: j for j in json.loads(OUTPUT.read_text())['jobs']}
        except (ValueError, KeyError):
            pass
    jobs = list(dedup.values())
    for job in jobs:
        job['first_seen'] = previous.get(job['id'], {}).get('first_seen', now)
        job['last_seen'] = now
    jobs.sort(key=lambda j: (-j['score'], j['age_days'] if j['age_days'] is not None else 999, j['company']))
    # Keep a meaningful Canadian sample, not only high-paying US SWE postings.
    canada = [j for j in jobs if j['country'] in ('Canada', 'US / Canada')][:120]
    usa = [j for j in jobs if j['country'] == 'United States'][:240]
    selected = sorted(canada + usa, key=lambda j: (-j['score'], j['age_days'] if j['age_days'] is not None else 999))
    payload = {'generated_at': now, 'term': 'Summer 2027', 'total': len(selected),
               'method': 'Aggregated job leads, not confirmation that applications are open',
               'sources': sources, 'jobs': selected}
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(f'{now}: {len(selected)} summer 2027 leads; {len(canada)} Canada and {len(usa)} US')


if __name__ == '__main__':
    main()
