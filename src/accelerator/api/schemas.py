"""Pydantic DTOs for FastAPI requests and responses."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    username: str
    role: str
    permissions: list[str]


class CreateUserRequest(BaseModel):
    username: str
    email: str
    full_name: str = ""
    role_name: str = "viewer"
    password: str


class RegisterProjectYamlRequest(BaseModel):
    raw_yaml: str


class SecretSetRequest(BaseModel):
    reference: str
    value: str


class DiscoverRequest(BaseModel):
    catalog_bundle_override: str | None = None
    extra_etl_paths: list[str] = Field(default_factory=list)


class ConvertRequest(BaseModel):
    wave_id: str | None = None
    object_key: str | None = None
    generator_name: str | None = None
    deliver_git: bool = False
    git_repo_path: str | None = None


class MoveRequest(BaseModel):
    wave_id: str | None = None
    object_key: str | None = None
    mode: str = "bulk"
    resume_load_run_id: str | None = None


class ReconcileRequest(BaseModel):
    wave_id: str | None = None
    object_key: str | None = None
    level_override: str | None = None


class ResidueDecisionRequest(BaseModel):
    decision: str  # approved | rejected
    replacement_code: str | None = None
    comment: str = ""


class DeployRequest(BaseModel):
    comment: str = "Engineer approved for deployment"


class SignoffRequest(BaseModel):
    object_key: str
    notes: str = "Reconciliation verified and approved"
    recon_run_id: str | None = None
    extra_details: dict[str, Any] = Field(default_factory=dict)
