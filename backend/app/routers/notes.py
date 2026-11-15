# Notes endpoints: every query is scoped to the logged-in user (other users' notes look like 404).
from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import get_current_user
from app.models import Note, User
from app.schemas import NoteCreate, NoteOut

router = APIRouter(prefix="/api/notes", tags=["notes"])


@router.get("", response_model=list[NoteOut])
def list_notes(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    query = select(Note).where(Note.owner_id == user.id).order_by(Note.id.desc())
    return db.scalars(query).all()


@router.post("", response_model=NoteOut, status_code=201)
def create_note(payload: NoteCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    note = Note(title=payload.title, body=payload.body, owner_id=user.id)
    db.add(note)
    db.commit()
    db.refresh(note)
    return note


@router.delete("/{note_id}", status_code=204)
def delete_note(note_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    note = db.scalar(select(Note).where(Note.id == note_id, Note.owner_id == user.id))
    if note is None:
        raise HTTPException(status_code=404, detail="Note not found")
    db.delete(note)
    db.commit()
    return Response(status_code=204)
