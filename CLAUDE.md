# CLAUDE.md

Guidance for AI assistants working in the `nova-mind-ai1` repository.

## Current state: empty repository

This repository has **no commits and no files**. There is nothing to analyze,
build, or run. This file is the first content added to it.

## Where the real code lives

The working NOVA MIND AI product lives in **`alrafi5240-oss/nova-mind-ai`**:

- FastAPI backend (Python 3.11 + OpenAI) — `src/main.py`
- React 18 + Vite + Tailwind frontend — `src/main.jsx`
- Electron desktop shell — `electron/main.cjs`

That repository has a detailed `CLAUDE.md` covering its architecture,
conventions, and known issues. Read it before doing any NOVA MIND work.

A third repo, `alrafi5240-oss/frontend-clean`, is an empty Vite/React scaffold
(zero-byte `index.html` and `src/main.jsx`).

## If you are asked to build here

Ask the user what this repository is for before writing anything. Given the
name, the likely intents are a fork, a rewrite, or a v2 of `nova-mind-ai` — but
nothing in the repository indicates which, and guessing wrong means building the
wrong thing. Once the purpose is settled, initialize the project structure to
match, and commit to the assigned feature branch rather than `main`.
