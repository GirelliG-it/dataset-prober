# dataset-prober
 A Python tool that probes open datasets via DuckDB httpfs and uses Claude to generate data quality assesments.


## How it works
The tool runs in two stages. First, prober.py queries each dataset URL 
directly using DuckDB's httpfs extension — no file is downloaded. It 
returns row counts, column names, data types, and sample rows. Second, 
agent.py feeds those results to Claude, which generates a plain-language 
analysis of each dataset.


## Components
- prober.py: handles both the connection test and the data inspection. It hits the URL, checks if it responds, counts rows, reads column names and types, pulls sample rows. All the structural work.

- agent.py: this script takes those results and passes them to Claude, which then reasons about what it found — is it analysis-ready, what's it useful for, what are the data quality issues. For each dataset that returns with status 'ok', it provides:
    1. A one-sentence description of what the dataset contains
    2. Its likely use case for public sector analysis
    3. A readiness assessment: is it analysis-ready?


