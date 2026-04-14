"""
Integration tests for using decorators.db inside a FastAPI application.

These tests exercise a realistic API lifecycle: schema startup, user creation,
password hashing, login, update, retrieval, and deletion.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from pydantic import BaseModel

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi import FastAPI, HTTPException
import httpx

from decorators.db import (
    RecordNotFoundError,
    UniqueConstraintError,
    database_registry,
    dispose_all,
    is_password_hash,
)


class UserCreate(BaseModel):
    email: str
    name: str
    password: str


class UserUpdate(BaseModel):
    name: str | None = None
    password: str | None = None


class LoginInput(BaseModel):
    email: str
    password: str


class UserOut(BaseModel):
    id: int
    email: str
    name: str


@pytest.fixture(autouse=True)
def _dispose_engines():
    yield
    dispose_all()


def _url(tmp_path: Path, name: str = "fastapi") -> str:
    return f"sqlite:///{tmp_path / f'{name}.db'}"


@pytest.mark.anyio
async def test_fastapi_full_user_lifecycle_with_password_auth(tmp_path):
    url = _url(tmp_path)

    @database_registry(
        url,
        table_name="users",
        key_field="id",
        auto_create=False,
        unique_fields=["email"],
    )
    class User(BaseModel):
        id: int | None = None
        email: str
        name: str
        password: str

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if not User.schema_exists():
            User.create_schema()
        yield
        User.objects.dispose()

    app = FastAPI(lifespan=lifespan)

    @app.post("/users", response_model=UserOut, status_code=201)
    def create_user(payload: UserCreate):
        try:
            user = User.objects.create(**payload.model_dump())
        except UniqueConstraintError:
            raise HTTPException(status_code=409, detail="Email already exists")
        return UserOut(id=user.id, email=user.email, name=user.name)

    @app.post("/auth/login")
    def login(payload: LoginInput):
        user = User.objects.get(email=payload.email)
        if user is None or not user.verify_password(payload.password):
            raise HTTPException(status_code=401, detail="Invalid credentials")
        return {"user_id": user.id}

    @app.get("/users/{user_id}", response_model=UserOut)
    def get_user(user_id: int):
        try:
            user = User.objects.require(user_id)
        except RecordNotFoundError:
            raise HTTPException(status_code=404, detail="User not found")
        return UserOut(id=user.id, email=user.email, name=user.name)

    @app.patch("/users/{user_id}", response_model=UserOut)
    def update_user(user_id: int, payload: UserUpdate):
        try:
            user = User.objects.require(user_id)
        except RecordNotFoundError:
            raise HTTPException(status_code=404, detail="User not found")

        updates = payload.model_dump(exclude_none=True)
        for field_name, value in updates.items():
            setattr(user, field_name, value)
        user.save()
        return UserOut(id=user.id, email=user.email, name=user.name)

    @app.delete("/users/{user_id}", status_code=204)
    def delete_user(user_id: int):
        try:
            user = User.objects.require(user_id)
        except RecordNotFoundError:
            raise HTTPException(status_code=404, detail="User not found")
        user.delete()
        return None

    assert User.schema_exists() is False

    async with app.router.lifespan_context(app):
        assert User.schema_exists() is True
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            create = await client.post(
                "/users",
                json={
                    "email": "alice@example.com",
                    "name": "Alice",
                    "password": "secret123",
                },
            )
            assert create.status_code == 201, create.text
            created = create.json()
            assert created["email"] == "alice@example.com"
            assert created["name"] == "Alice"
            assert "password" not in created
            user_id = created["id"]

            stored = User.objects.require(user_id)
            assert stored.password != "secret123"
            assert is_password_hash(stored.password)
            assert stored.verify_password("secret123") is True

            duplicate = await client.post(
                "/users",
                json={
                    "email": "alice@example.com",
                    "name": "Alice Two",
                    "password": "secret123",
                },
            )
            assert duplicate.status_code == 409

            bad_login = await client.post(
                "/auth/login",
                json={"email": "alice@example.com", "password": "wrong"},
            )
            assert bad_login.status_code == 401

            good_login = await client.post(
                "/auth/login",
                json={"email": "alice@example.com", "password": "secret123"},
            )
            assert good_login.status_code == 200
            assert good_login.json() == {"user_id": user_id}

            get_user = await client.get(f"/users/{user_id}")
            assert get_user.status_code == 200
            assert get_user.json() == {
                "id": user_id,
                "email": "alice@example.com",
                "name": "Alice",
            }

            update = await client.patch(
                f"/users/{user_id}",
                json={"name": "Alicia", "password": "new-secret"},
            )
            assert update.status_code == 200
            assert update.json() == {
                "id": user_id,
                "email": "alice@example.com",
                "name": "Alicia",
            }

            refreshed = User.objects.require(user_id)
            assert refreshed.password != stored.password
            assert refreshed.verify_password("secret123") is False
            assert refreshed.verify_password("new-secret") is True

            old_login = await client.post(
                "/auth/login",
                json={"email": "alice@example.com", "password": "secret123"},
            )
            assert old_login.status_code == 401

            new_login = await client.post(
                "/auth/login",
                json={"email": "alice@example.com", "password": "new-secret"},
            )
            assert new_login.status_code == 200

            deleted = await client.delete(f"/users/{user_id}")
            assert deleted.status_code == 204

            missing = await client.get(f"/users/{user_id}")
            assert missing.status_code == 404
