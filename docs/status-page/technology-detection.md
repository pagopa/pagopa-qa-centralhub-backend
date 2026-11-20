# Status page: technology detection

Each application on the status page can show a technology badge such as `JAVA 17` or
`PYTHON 3.12`. This page explains where the language and the version come from.

The code lives in `app/services/status_page_stack.py`. The detection runs in the enrichment
task (`app/services/status_page_enrich.py`), and the result is cached in `sp_tech_stacks`.

## Principle: reliable data or nothing

- If a value is not certain, it is not shown. The badge shows the language alone (e.g. `JAVA`),
  or no badge is shown at all.
- GitHub's language statistics are never used.
- The badge tooltip says where the value comes from ("Rilevato da Dockerfile" or "da file di
  build") and at which release.

## What is read

Everything is read from the repository at the tag of its current release (the REPO version
shown on the status page). Only files in the **repository root** are considered.

### 1. Language

The language is identified by the build files in the root:

| Language | Files that identify it |
|---|---|
| JAVA | `pom.xml`, `build.gradle`, `build.gradle.kts` |
| PYTHON | `pyproject.toml`, `requirements.txt` |
| NODE | `package.json` |
| RUST | `Cargo.toml` |
| GO | `go.mod` |

When the root holds build files of more than one language, the Dockerfile runtime image decides.
If it cannot, the result is `UNAVAILABLE` and no badge is shown.

### 2. Version

The version is looked up in this order, and the first reliable value wins:

1. **Dockerfile**: only the **last `FROM`** line (the runtime stage); build stages are ignored.
   An image given through a variable (`FROM ${IMAGE}`) is not resolved.
2. **Build files**: in the order listed in the table below.

| Language | From the Dockerfile (last `FROM`) | From the build files, in order | Format |
|---|---|---|---|
| JAVA | Major in the image name: `openjdk17`, `openjdk-17`, `openjdk-21-runtime`. Otherwise, only for the official images `eclipse-temurin`, `amazoncorretto` and `openjdk`, the tag (e.g. `21-jre` → 21). A major below 8 is discarded. | `pom.xml`: `java.version`, then `maven.compiler.release`, then `maven.compiler.source`. `build.gradle(.kts)`: `JavaLanguageVersion.of(N)`, then `sourceCompatibility`. | major (`1.8` → `8`) |
| PYTHON | Tag of `python` (e.g. `3.12-slim` → 3.12) | `pyproject.toml`: `requires-python` (`>=3.11`, `~=3.11`, `^3.11`, `==3.11`). `.python-version`. | major.minor |
| NODE | Tag of `node` (e.g. `20-alpine` → 20) | `package.json`: `engines.node`. `.nvmrc`. | major |
| RUST | Tag of `rust` (e.g. `1.75-slim` → 1.75) | `Cargo.toml`: `rust-version`. `rust-toolchain.toml`, `rust-toolchain`. | major.minor |
| GO | Tag of `golang` (e.g. `1.22-alpine` → 1.22) | `go.mod`: `go` directive | major.minor |

For a derived image whose tag is its own release (e.g. Red Hat
`ubi8/openjdk-17:1.14`, where `1.14` is not the Java version), the tag is never used: the
version comes from the image name or, failing that, from the build files.

### Values that are not reliable

These give no version, so the badge shows the language alone, unless a later source in the
order above gives one:

- a tag without a version: `latest`, `jre`, or a major-only Python tag such as `python:3`;
- a range in `requires-python`, such as `>=3.10,<4`;
- a property in `pom.xml`, such as `<java.version>${x}</java.version>`;
- a version given through a Dockerfile variable.

## When it is refreshed

- After each status page sync, the enrichment task detects the stack of every repository whose
  REPO version changed since the last detection (that is, at each new release).
- Failed detections (`ERROR`) are retried up to 3 times.
- The task shares the `GITHUB_TOKEN` budget with the version sync: it stops when the remaining
  rate limit falls below `STATUS_PAGE_GITHUB_RESERVE` (default 1000).
- To force a new detection, delete the repository's row from `sp_tech_stacks`. The next run
  detects it again. Migration `0035` did this for the Java stacks read from a Dockerfile.

## Known limits

- Only the repository root is read, so a project with its `pom.xml` or `Dockerfile` in a
  subfolder (e.g. a monorepo) is not recognized.
- Languages other than the five above are not detected.
