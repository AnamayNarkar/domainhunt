# domainhunt

Finds **single English words that are still available as `.com` (and `.io`)** domains. It walks a filtered English
dictionary, asks Cloudflare's Registrar API whether each domain is registrable, and keeps the hits.
It only *checks* availability - it never registers or buys anything.

Honest expectation: nearly every common word is already registered. Expect a trickle of results, mostly rarer words
(and mostly `.io`). The tool's value is checking thousands of words for you at no cost.

Two ways to run it:

| | What | When |
|---|---|---|
| [`cli/`](cli) | Local Python CLI, SQLite, resumable, ranked output | Easiest; run on your machine |
| [`aws/`](aws) | Lambda + EventBridge + S3 static page that runs unattended and shows a live results page | You want a URL to check now and then |

## How it works (short)
1. **Candidates**: words come from WordNet (a real dictionary) and wordfreq (how commonly a word is used). They are
   filtered to lowercase a-z, 4-10 letters, a frequency band (skip ultra-common words, which are always taken, and
   too-obscure ones), and WordNet lemmas only. Plurals/-ed/-ing forms, proper nouns, profanity and words whose only
   meanings are medical/biological/chemical are dropped. CMUdict adds syllable counts; a documented 0-100 brand score
   ranks what is left (length, frequency, syllables, pronounceability, meaning).
2. **Check**: batches of 20 domains go to Cloudflare `POST /accounts/{id}/registrar/domain-check`
   (max 20 per request, ~30 requests/minute). `registrable: true` means available; the response also gives
   registration/renewal prices.
3. **Store/output**: the CLI keeps everything in SQLite (resumable; adding a TLD only checks the new domains) and exports
   `available.csv`. The AWS version appends hits to `results.json` in S3, which a static page displays.

## Requirements
- A Cloudflare account with an **API token** that can use the Registrar domain-check endpoint, and your **Account ID**.
  Create a token at dash.cloudflare.com -> My Profile -> API Tokens. Checking is free.
- Python 3.9+.

## Option A: local CLI
```bash
cd cli
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export CF_ACCOUNT_ID=your_account_id
export CF_API_TOKEN=your_token

python domainhunt.py --dry-run            # preview candidates + per-stage counts, no API calls -> out/candidates.csv
python domainhunt.py --limit 500          # check the best 500 unchecked domains
python domainhunt.py --tlds com io ai --profile rare --recheck 30
python domainhunt.py --export             # rebuild out/available.csv from the database
pytest -q                                 # tests (Cloudflare is mocked)
```
Results are in `out/domainhunt.db` and `out/available.csv` (one row per available domain, `.com` first).

Useful options: `--tlds`, `--profile common|balanced|rare` (minimum word frequency), `--min-zipf/--max-zipf`,
`--min-len/--max-len`, `--limit`, `--seed`, `--delay` (default 2.1s), `--retry-limit`, `--recheck DAYS`,
`--import-legacy DIR` (imports an old `available.csv`; the old `seen.txt` is deliberately ignored).

**Brand score (0-100):** length 20 (5-8 letters best) + frequency 25 (best at Zipf 3-4) + syllables 20 (2 best) +
pronounceability 10 + meaning 25 (reduced for technical senses). Per-word components are in `candidates.csv`.

**Errors/rate limits:** 429 honours `Retry-After`; 5xx/network errors back off exponentially with jitter; 401/403 stop
the run; a domain missing from an API response is retried, never assumed taken.

## Option B: hosted on AWS (about $0.50/month)
EventBridge fires a Lambda every minute; it checks ~120 words and appends hits to `results.json` in a public-read
S3 bucket; `index.html` (with `.com`/`.io` filters and a copy-names button) renders it. When every word has been
checked the Lambda deletes its own schedule.

```bash
pip install -r cli/requirements.txt          # needed to build the word list
export CF_ACCOUNT_ID=... CF_API_TOKEN=...    # AWS CLI must already be configured for your account
export TEARDOWN_PASSWORD='long-random-string'   # optional: enables the "Delete all resources" button
bash aws/deploy.sh
```
It prints the site URL. Resources created (all named `domainhunt*`): S3 bucket, 2 Lambdas, 2 IAM roles, 1 EventBridge
rule, CloudWatch log groups.

**Read before deploying:**
- The Cloudflare token is stored as a Lambda environment variable.
- The bucket serves `index.html` and `results.json` publicly (the `state/` folder stays private).
- The delete button calls a **public Lambda URL protected only by `TEARDOWN_PASSWORD`**. Use a long random password,
  or omit `TEARDOWN_PASSWORD` to skip the button and delete resources yourself.
- `deploy.sh` was assembled from commands that were run by hand against a real account; it has been syntax-checked
  but not run end-to-end as a single script. Review it before use.
- Known limitation: `aws/handler.py` treats a domain missing from an API response as checked (the CLI handles this
  correctly by retrying).
- The IAM user running `deploy.sh` needs permission to create roles, Lambdas, S3 buckets and EventBridge rules.

## Layout
```
cli/domainhunt.py        the CLI (candidate pipeline, API client, SQLite)
cli/test_domainhunt.py   tests with a mocked Cloudflare API
aws/handler.py           checker Lambda
aws/teardown.py          optional delete-everything Lambda
aws/index.html           results page
aws/deploy.sh            deployment script
```

## Data sources
Princeton WordNet (via NLTK), wordfreq, CMU Pronouncing Dictionary (via `cmudict`). The Oxford English Dictionary is
proprietary and not available as a free library.

## License
No license has been chosen yet (private repository).
