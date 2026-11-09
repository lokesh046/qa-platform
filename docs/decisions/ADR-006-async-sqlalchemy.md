# ADR-006: The backend uses SQLAlchemy's async engine

Date: 2026-11-08
Status: Accepted

## Context

Step 3 creates the database session infrastructure, which fixes whether the
whole backend is written `async def` or `def`. Changing it later means
rewriting every repository, service, and router, so the choice is effectively
permanent.

The surrounding facts:

- FastAPI is an async framework. A `def` endpoint is run in a thread from a
  bounded pool; an `async def` endpoint runs on the event loop.
- The work this service does is almost entirely waiting on the network. A
  request to `GET /issues/{key}` waits on Postgres, then possibly on the Jira
  REST API. Very little CPU is involved.
- Jira is called with `httpx`, which is async.
- Later phases add LangGraph workflows and long-running Playwright executions,
  both of which fan out to several I/O-bound calls at once.
- `psycopg[binary]` version 3 is installed, and SQLAlchemy's
  `postgresql+psycopg` dialect drives **both** a blocking and an async engine
  from the same URL string.

## Decision

The application uses `create_async_engine` and `AsyncSession`. Endpoints,
services, and repositories are `async def`.

- `app/core/database.py` owns the engine, the `async_sessionmaker`, and the
  `get_session` FastAPI dependency.
- The engine is built lazily behind an `lru_cache`, so importing a module does
  not open a socket.
- `expire_on_commit=False` on the session factory. With SQLAlchemy's default,
  every attribute is invalidated on commit, and the implicit refresh that
  follows raises `MissingGreenlet` in async code instead of silently issuing a
  SELECT. Turning it off removes a confusing failure mode.
- `pool_pre_ping=True`, which spends one cheap round trip per checkout and in
  exchange survives Postgres closing an idle connection.
- Alembic, in Step 5, uses a **blocking** engine built from the same URL.
  Migrations are a serial batch job; async buys nothing there and costs an
  event-loop wrapper around every revision.

`sqlalchemy[asyncio]` is the dependency, not plain `sqlalchemy`. The extra
pulls in `greenlet`, which the async engine requires at import time. This was
found the hard way: the first run of the Step 3 verification failed with
`ImportError: The SQLAlchemy asyncio module requires that the Python 'greenlet'
library is installed`.

## Alternatives

- **Blocking SQLAlchemy with `def` endpoints.** Simpler, and genuinely the
  right answer for many services: no greenlet, no `MissingGreenlet`, lazy
  loading works normally, and the ecosystem of examples is larger. Rejected
  because the Jira client is async, so a blocking endpoint would have to bridge
  into the event loop with `anyio.from_thread` or similar on every outbound
  call. Mixing the two directions is harder to reason about than committing to
  one, and the thread pool caps concurrency at a number unrelated to how many
  requests are actually waiting on Jira.
- **Blocking SQLAlchemy run in a threadpool via `run_in_executor`.** All the
  complexity of async with none of the clarity.
- **`asyncpg` instead of psycopg3.** Faster on paper. Rejected because psycopg3
  serves the sync engine Alembic needs from the same URL, while asyncpg would
  mean two drivers and two URL schemes in the configuration, and a second thing
  to get wrong in `.env`. Raw driver throughput is not this service's
  bottleneck; Jira's response time is.
- **An async ORM such as Tortoise or SQLModel.** Tortoise means leaving the
  SQLAlchemy and Alembic ecosystem the brief specifies. SQLModel is a thin
  layer over SQLAlchemy that blurs the line between table models and API
  schemas, which is exactly the line the module pattern keeps sharp.

## Consequences

- Every repository and service in this project is `async def`. A blocking call
  inside one — `requests`, `time.sleep`, a large `json.loads`, an unguarded
  file read — stalls the entire event loop, not just that request. This is the
  main hazard of the decision and it needs to be a review habit.
- Lazy relationship loading is unavailable in the usual way. An unloaded
  attribute touched outside a greenlet context raises `MissingGreenlet` rather
  than issuing a query. Relationships must be loaded eagerly with
  `selectinload` or `joinedload`, which Step 5's models and Step 6's queries
  must state explicitly. This is more verbose and, on balance, a good thing: it
  makes N+1 query patterns visible at the call site.
- `greenlet` is a compiled dependency. It has prebuilt wheels for CPython on
  macOS arm64 and Linux, so this is not currently a problem, but it is one more
  thing that must build if a platform ever lacks a wheel.
- Tests need an async runner. `pytest-asyncio` is already a dev dependency with
  `asyncio_mode = "auto"`, so an `async def test_...` just works.
- The pool is per process. With `uvicorn --workers N`, real connection usage is
  `N * (db_pool_size + db_max_overflow)`. At the defaults of 5 and 10 that is 15
  per worker, so four workers can demand 60 connections against a Postgres
  default `max_connections` of 100. Worth sizing deliberately before anything is
  deployed.
- Alembic's `env.py` in Step 5 must use `create_engine`, not
  `create_async_engine`, and must import the URL through
  `app.core.database.sync_database_url`.
