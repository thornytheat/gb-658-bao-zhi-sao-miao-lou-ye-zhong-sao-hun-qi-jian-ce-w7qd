"""M7 留存与销毁接口。

- POST /api/retention/receive   接收真实清单，登记保留期限，签发验收凭据
- POST /api/retention/destroy   到期销毁原文与隔离原值，支持中断恢复
- POST /api/retention/verify    提交原件验真，不落库
- GET  /api/retention/{id}      回读保留状态与原接收数量
"""
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from app.web import page
from . import service

router = APIRouter(prefix="/api/retention", tags=["m7-retention"])
page_router = APIRouter(tags=["m7-retention-pages"])


class ReceiveRequest(BaseModel):
    request_id: str = Field(min_length=1)
    manifest_text: str
    retention_days: int = 30


class DestroyRequest(BaseModel):
    request_id: str = Field(min_length=1)
    retention_id: str | None = None
    force: bool = False


class VerifyRequest(BaseModel):
    request_id: str = Field(min_length=1)
    retention_id: str
    manifest_text: str


@page_router.get("/retention", response_class=HTMLResponse)
def view(request: Request):
    rows = [
        {"key": "ret-a1b2c3d4", "status": "留存中", "value": "12"},
        {"key": "ret-e5f6a7b8", "status": "销毁中", "value": "7"},
        {"key": "ret-c9d0e1f2", "status": "已销毁", "value": "20"},
    ]
    return page(request, "m7.html", "留存销毁", "/api/retention/receive", rows)


@router.post("/receive")
def receive(body: ReceiveRequest):
    return service.receive(
        request_id=body.request_id,
        manifest_text=body.manifest_text,
        retention_days=body.retention_days,
    )


@router.post("/destroy")
def destroy(body: DestroyRequest):
    return service.destroy(
        request_id=body.request_id,
        retention_id=body.retention_id,
        force=body.force,
    )


@router.post("/verify")
def verify(body: VerifyRequest):
    return service.verify_original(
        request_id=body.request_id,
        retention_id=body.retention_id,
        manifest_text=body.manifest_text,
    )


@router.get("/{retention_id}")
def read_status(retention_id: str):
    return service.read_status(retention_id)
