# Full-Stack Monorepo Rules (FastAPI + Next.js)

## Architecture & Ports
- **`/backend`**: Python FastAPI on `http://127.0.0.1:8000` with SQLite (`backend/data/app.db`) + SQLAlchemy 2.0.
- **`/frontend`**: Next.js 16 (App Router, TypeScript, Tailwind CSS v4, Shadcn UI) on `http://localhost:3000`.
- **API Proxy**: `frontend/next.config.ts` proxies `/api/*` directly to `http://127.0.0*` so `httpOnly` JWT cookies work without CORS issues.

## Exact Backend Endpoints & Conventions
- **Auth (`/api/auth`)**:
  - `POST /api/auth/register` `{ email, password }` -> `201 UserOut`
  - `POST /api/auth/login` `{ email, password }` -> `200 UserOut` (sets `httpOnly` JWT cookie)
  - `POST /api/auth/logout` -> `200` (clears cookie)
  - `GET /api/auth/me` -> `200 UserOut`
- **Notes Example (`/api/notes`)**:
  - `GET /api/notes` -> `list[NoteOut]`
  - `POST /api/notes` `{ title, body }` -> `201 NoteOut`
  - `DELETE /api/notes/{note_id}` -> `204`
- **Backend File Layout**:
  - Models: `backend/app/models.py`
  - Pydantic Schemas: `backend/app/schemas.py`
  - Auth Dependency: `user: User = Depends(get_current_user)` in `backend/app/deps.py`
  - Routers: `backend/app/routers/*.py` (register new routers in `backend/main.py`)

## Frontend Conventions
- Always use `apiFetch` from `@/lib/api` (includes `credentials: 'include'` and JSON headers).
- Use existing Shadcn UI components from `@/components/ui/*` before writing custom HTML inputs/buttons/cards.
- Keep all pages simple inside `frontend/src/app/` (no `[locale]` routing).

## Verification Rules
- Propose a brief file-by-file plan before making multi-file edits.
- After backend edits: run `pytest` inside `/backend`.
- After frontend edits: run `npx tsc --noEmit` inside `/frontend`.
