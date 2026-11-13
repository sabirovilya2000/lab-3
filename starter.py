from datetime import datetime
from enum import Enum
from typing import Annotated, Optional
from functools import lru_cache

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

# --- 1. Модели и Настройки ---

class Priority(str, Enum):
    low = "low"
    normal = "normal"
    high = "high"
    urgent = "urgent"

class TicketStatus(str, Enum):
    open = "open"
    in_progress = "in_progress"
    resolved = "resolved"
    closed = "closed"

class Settings(BaseModel):
    default_page_size: int = 5
    max_page_size: int = 20

class TicketCreate(BaseModel):
    title: str = Field(min_length=1)
    description: Optional[str] = None
    priority: Priority = Priority.normal
    assignee: Optional[str] = None

class TicketUpdate(BaseModel):
    description: Optional[str] = None
    priority: Optional[Priority] = None
    assignee: Optional[str] = None
    status: Optional[TicketStatus] = None

class Ticket(BaseModel):
    id: int
    title: str
    description: Optional[str]
    priority: Priority
    assignee: Optional[str]
    status: TicketStatus
    created_at: datetime

class TicketPage(BaseModel):
    items: list[Ticket]
    total: int
    page: int
    page_size: int

# --- 2. Исключения ---

class TicketNotFoundError(Exception):
    def __init__(self, ticket_id: int):
        self.ticket_id = ticket_id

class InvalidStatusTransitionError(Exception):
    def __init__(self, current: str, new: str):
        self.current = current
        self.new = new

# --- 3. Слой данных (Repository) ---

class TicketRepository:
    def __init__(self):
        self._tickets: dict[int, Ticket] = {}
        self._next_id = 1

    def next_id(self) -> int:
        current = self._next_id
        self._next_id += 1
        return current

    def add(self, ticket: Ticket) -> Ticket:
        self._tickets[ticket.id] = ticket
        return ticket

    def get(self, ticket_id: int) -> Ticket:
        if ticket_id not in self._tickets:
            raise TicketNotFoundError(ticket_id)
        return self._tickets[ticket_id]

    def list_all(self) -> list[Ticket]:
        return list(self._tickets.values())

    def update(self, ticket: Ticket) -> Ticket:
        if ticket.id not in self._tickets:
            raise TicketNotFoundError(ticket.id)
        self._tickets[ticket.id] = ticket
        return ticket

    def delete(self, ticket_id: int) -> None:
        if ticket_id not in self._tickets:
            raise TicketNotFoundError(ticket_id)
        del self._tickets[ticket_id]

# --- 4. Слой бизнес-логики (Service) ---

ALLOWED_TRANSITIONS = {
    TicketStatus.open: [TicketStatus.in_progress],
    TicketStatus.in_progress: [TicketStatus.resolved, TicketStatus.open],
    TicketStatus.resolved: [TicketStatus.closed, TicketStatus.in_progress],
    TicketStatus.closed: []
}

class TicketService:
    def __init__(self, repository: TicketRepository, settings: Settings):
        self.repository = repository
        self.settings = settings

    def create_ticket(self, payload: TicketCreate) -> Ticket:
        ticket = Ticket(
            id=self.repository.next_id(),
            title=payload.title,
            description=payload.description,
            priority=payload.priority,
            assignee=payload.assignee,
            status=TicketStatus.open,
            created_at=datetime.now()
        )
        return self.repository.add(ticket)

    def get_ticket(self, ticket_id: int) -> Ticket:
        return self.repository.get(ticket_id)

    def list_tickets(self, sort_by: str, order: str, page: int, page_size: Optional[int]) -> TicketPage:
        tickets = self.repository.list_all()

        if page_size is None:
            page_size = self.settings.default_page_size
        else:
            page_size = min(page_size, self.settings.max_page_size)

        if sort_by == "created_at":
            tickets.sort(key=lambda x: x.created_at, reverse=(order == "desc"))
        elif sort_by == "priority":
            priority_order = {Priority.low: 1, Priority.normal: 2, Priority.high: 3, Priority.urgent: 4}
            tickets.sort(key=lambda x: priority_order[x.priority], reverse=(order == "desc"))

        total = len(tickets)
        start = (page - 1) * page_size
        end = start + page_size
        items = tickets[start:end]

        return TicketPage(items=items, total=total, page=page, page_size=page_size)

    def update_ticket(self, ticket_id: int, payload: TicketUpdate) -> Ticket:
        ticket = self.repository.get(ticket_id)
        update_data = payload.model_dump(exclude_unset=True)

        new_status = update_data.get("status")
        if new_status is not None and new_status != ticket.status:
            allowed = ALLOWED_TRANSITIONS.get(ticket.status, [])
            if new_status not in allowed:
                raise InvalidStatusTransitionError(ticket.status.value, new_status.value)

        for key, value in update_data.items():
            setattr(ticket, key, value)

        return self.repository.update(ticket)

    def delete_ticket(self, ticket_id: int) -> None:
        self.repository.delete(ticket_id)

# --- 5. Зависимости (Dependencies) ---

@lru_cache
def get_settings() -> Settings:
    return Settings()

@lru_cache
def get_repository() -> TicketRepository:
    return TicketRepository()

def get_service(
    repo: Annotated[TicketRepository, Depends(get_repository)],
    settings: Annotated[Settings, Depends(get_settings)]
) -> TicketService:
    return TicketService(repo, settings)

SettingsDep = Annotated[Settings, Depends(get_settings)]
ServiceDep = Annotated[TicketService, Depends(get_service)]

# --- 6. HTTP Слой (Приложение и Эндпоинты) ---

app = FastAPI(
    title="HelpDesk Lite",
    description="Внутренний сервис техподдержки",
    version="1.0.0"
)

@app.exception_handler(TicketNotFoundError)
async def not_found_handler(request: Request, exc: TicketNotFoundError):
    return JSONResponse(status_code=404, content={"detail": f"Тикет {exc.ticket_id} не найден"})

@app.exception_handler(InvalidStatusTransitionError)
async def invalid_transition_handler(request: Request, exc: InvalidStatusTransitionError):
    return JSONResponse(
        status_code=409,
        content={"detail": f"Переход {exc.current} -> {exc.new} запрещён"}
    )

@app.post("/tickets", response_model=Ticket, status_code=status.HTTP_201_CREATED, tags=["Тикеты"], summary="Создать тикет")
def create_ticket(payload: TicketCreate, service: ServiceDep):
    return service.create_ticket(payload)

@app.get("/tickets", response_model=TicketPage, tags=["Тикеты"], summary="Список тикетов")
def list_tickets(
    service: ServiceDep,
    sort_by: str = Query(default="created_at", pattern="^(created_at|priority)$"),
    order: str = Query(default="desc", pattern="^(asc|desc)$"),
    page: int = Query(default=1, ge=1),
    page_size: Optional[int] = Query(default=None, ge=1)
):
    return service.list_tickets(sort_by, order, page, page_size)

@app.get("/tickets/{ticket_id}", response_model=Ticket, tags=["Тикеты"], summary="Получить тикет по ID")
def get_ticket(ticket_id: int, service: ServiceDep):
    return service.get_ticket(ticket_id)

@app.patch("/tickets/{ticket_id}", response_model=Ticket, tags=["Тикеты"], summary="Обновить тикет")
def update_ticket(ticket_id: int, payload: TicketUpdate, service: ServiceDep):
    return service.update_ticket(ticket_id, payload)

@app.delete("/tickets/{ticket_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["Тикеты"], summary="Удалить тикет")
def delete_ticket(ticket_id: int, service: ServiceDep):
    service.delete_ticket(ticket_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)