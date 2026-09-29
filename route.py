"""
Маршрут новичка — за 5 дней до выхода на работу и до конца испытательного срока: этапы, напоминания, эскалации, метрики.

Всё здесь — Rules + Agent: детерминированный код без LLM. Агент напоминает и эскалирует,
но отметить этап выполненным может только тот, кто указан в confirm_by шаблона
(обязательное обучение сотрудник сам себе не засчитает).

    data/route_template.json   шаблон этапов (день относительно выхода, владелец, кто подтверждает)
    data/employees.json        сотрудники и статусы (создаётся из employees.example.json; в git не попадает)

CLI:
    python route.py status [--date 2026-10-05]      сводка по всем
    python route.py show smirnova                   маршрут сотрудника
    python route.py remind [--dry-run]              разослать (здесь — напечатать) напоминания на сегодня
    python route.py confirm smirnova safety_briefing --role от --by "Мария (ОТ)"
    python route.py add --id ivanov --name "Иван Иванов" --position Аналитик --department Финансы \\
                        --start 2026-10-12 --manager "Ольга Петрова" [--tg ivanov] [--manager-tg opetrova]
    python route.py metrics                         три метрики успеха онбординга
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

BASE = Path(__file__).parent
DATA = Path(os.getenv("ROUTE_DATA_DIR", BASE / "data"))
TEMPLATE = DATA / "route_template.json"
EMPLOYEES = DATA / "employees.json"
EXAMPLE = DATA / "employees.example.json"
TRACE = BASE / "logs" / "trace.jsonl"

AHEAD_DAYS = int(os.getenv("REMIND_AHEAD_DAYS", "2"))     # за сколько дней предупреждать
ESCALATE_DAYS = int(os.getenv("ESCALATE_AFTER_DAYS", "3"))  # просрочка → эскалация в HR
STAFF_ROLES = {"hr", "it", "иб", "от", "руководитель"}

ICONS = {"done": "✅", "late_done": "☑️", "overdue": "❗", "today": "⏰", "soon": "🔜", "upcoming": "⬜"}
ROLE_NAMES = {"сотрудник": "вы", "руководитель": "руководитель", "hr": "HR", "it": "IT", "иб": "ИБ",
              "от": "служба ОТ"}


# ---------------------------------------------------------------- данные

def _resolve(value: str, today: date) -> str:
    """'+3' / '-10' → ISO-дата относительно today; ISO-дата остаётся как есть."""
    v = str(value).strip()
    if v[:1] in "+-" and v[1:].isdigit():
        return (today + timedelta(days=int(v))).isoformat()
    return v


class Store:
    """JSON-хранилище сотрудников. Потокобезопасно: бот пишет из обработчиков и из потока напоминаний."""

    def __init__(self, path: Path = EMPLOYEES, template: Path = TEMPLATE):
        self.path = path
        self.stages = json.loads(template.read_text(encoding="utf-8"))["stages"]
        self.by_id = {s["id"]: s for s in self.stages}
        self.lock = threading.RLock()
        if not path.exists():
            self._init_from_example()
        self.data = json.loads(path.read_text(encoding="utf-8"))

    def _init_from_example(self):
        today = date.today()
        src = json.loads(EXAMPLE.read_text(encoding="utf-8")) if EXAMPLE.exists() else {"employees": []}
        emps = {}
        for e in src["employees"]:
            e = dict(e)
            e["start_date"] = _resolve(e["start_date"], today)
            e["done"] = {k: {"at": _resolve(v, today), "by": "демо"} for k, v in e.get("done", {}).items()}
            emps[e["id"]] = e
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"employees": emps}, ensure_ascii=False, indent=2), encoding="utf-8")

    def save(self):
        with self.lock:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.path)

    @property
    def employees(self) -> dict:
        return self.data["employees"]

    def get(self, emp_id: str) -> dict:
        if emp_id not in self.employees:
            raise KeyError(f"нет сотрудника {emp_id}")
        return self.employees[emp_id]

    def find_by_tg(self, username: str | None) -> dict | None:
        u = (username or "").lstrip("@").lower()
        return next((e for e in self.employees.values() if u and e.get("tg", "").lower() == u), None)

    def managed_by(self, username: str | None) -> list[dict]:
        u = (username or "").lstrip("@").lower()
        return [e for e in self.employees.values() if u and e.get("manager_tg", "").lower() == u]

    def add(self, emp: dict):
        with self.lock:
            if emp["id"] in self.employees:
                raise ValueError(f"сотрудник {emp['id']} уже есть")
            date.fromisoformat(emp["start_date"])
            emp.setdefault("done", {})
            self.employees[emp["id"]] = emp
            self.save()

    def bind_chat(self, emp: dict, key: str, chat_id: int) -> bool:
        with self.lock:
            if emp.get(key) == chat_id:
                return False
            emp[key] = chat_id
            self.save()
            return True

    # ---------------------------------------------------------------- маршрут

    def route(self, emp: dict) -> list[dict]:
        """Персональный маршрут: этапы шаблона с учётом отдела."""
        return [s for s in self.stages if not s.get("departments") or emp.get("department") in s["departments"]]

    def confirm(self, emp_id: str, stage_id: str, role: str, by: str, when: date | None = None) -> dict:
        """Отметить этап. Роль проверяется по шаблону — это правило, а не просьба в промпте."""
        with self.lock:
            emp = self.get(emp_id)
            stage = next((s for s in self.route(emp) if s["id"] == stage_id), None)
            if not stage:
                raise KeyError(f"в маршруте {emp_id} нет этапа {stage_id}")
            if role not in stage["confirm_by"]:
                who = ", ".join(ROLE_NAMES.get(r, r) for r in stage["confirm_by"])
                raise PermissionError(f"этап «{stage['title']}» подтверждает: {who}")
            if stage_id in emp["done"]:
                raise ValueError(f"этап «{stage['title']}» уже отмечен")
            emp["done"][stage_id] = {"at": (when or date.today()).isoformat(), "by": by, "role": role}
            self.save()
            return stage


def due_date(emp: dict, stage: dict) -> date:
    return date.fromisoformat(emp["start_date"]) + timedelta(days=stage["day"])


def opens(emp: dict, stage: dict) -> date:
    """С какого дня этап можно отметить: до выхода — в свой день, после выхода — с первого дня работы."""
    return due_date(emp, stage) if stage["day"] <= 0 else date.fromisoformat(emp["start_date"])


def stage_status(emp: dict, stage: dict, today: date) -> str:
    due = due_date(emp, stage)
    if stage["id"] in emp.get("done", {}):
        return "done" if date.fromisoformat(emp["done"][stage["id"]]["at"]) <= due else "late_done"
    if today > due:
        return "overdue"
    if today == due:
        return "today"
    if (due - today).days <= AHEAD_DAYS:
        return "soon"
    return "upcoming"


def format_route(store: Store, emp: dict, today: date, self_view: bool = False) -> str:
    start = date.fromisoformat(emp["start_date"])
    lines = [f"📋 Маршрут: {emp['name']} · {emp['position']}, {emp['department']}",
             f"Выход: {start:%d.%m.%Y} · руководитель: {emp.get('manager', '—')}", ""]
    for s in store.route(emp):
        st = stage_status(emp, s, today)
        owner = ROLE_NAMES.get(s["owner"], s["owner"]) if self_view else s["owner"]
        tail = "" if s.get("mandatory", True) else " (по желанию)"
        extra = f" · id: {s['id']}" if not self_view else ""
        lines.append(f"{ICONS[st]} {due_date(emp, s):%d.%m} {s['title']}{tail} — {owner}{extra}")
    done = sum(s["id"] in emp["done"] for s in store.route(emp))
    lines += ["", f"Готово {done} из {len(store.route(emp))}. ❗ просрочено · ⏰ сегодня · 🔜 скоро"]
    return "\n".join(lines)


# ---------------------------------------------------------------- напоминания

@dataclass
class Reminder:
    to: str          # employee | manager | hr | security
    emp_id: str
    stage_id: str
    kind: str        # soon | today | overdue | escalation
    text: str


def _recipient(owner: str) -> str:
    return {"сотрудник": "employee", "руководитель": "manager", "иб": "security"}.get(owner, "hr")


def due_reminders(store: Store, today: date, mark: bool = True) -> list[Reminder]:
    """Детерминированные правила напоминаний. mark=True — запомнить отправленное, чтобы не дублировать."""
    out = []
    with store.lock:
        for emp in store.employees.values():
            sent = emp.setdefault("reminded", {})
            for s in store.route(emp):
                st = stage_status(emp, s, today)
                if st in ("done", "late_done", "upcoming"):
                    continue
                due = due_date(emp, s)
                late = (today - due).days
                to = _recipient(s["owner"])
                who = "" if to == "employee" else f"{emp['name']}: "
                plan = []
                if st == "soon":
                    plan.append(("soon", to, f"🔜 {who}до {due:%d.%m} — {s['title']}", False))
                elif st == "today":
                    plan.append(("today", to, f"⏰ {who}сегодня — {s['title']}", False))
                else:
                    plan.append(("overdue", to, f"❗ {who}просрочено на {late} дн. — {s['title']}", True))
                    if late >= ESCALATE_DAYS and s.get("mandatory", True):
                        plan.append(("escalation", "hr",
                                     f"🚨 Эскалация: {emp['name']} — «{s['title']}» просрочен на {late} дн. "
                                     f"(исполнитель: {s['owner']}, подтверждает: {', '.join(s['confirm_by'])})", False))
                for kind, rcpt, text, daily in plan:
                    key = f"{s['id']}:{kind}"
                    if (sent.get(key) == today.isoformat()) if daily else (key in sent):
                        continue
                    if s.get("note") and rcpt == "employee":
                        text += f"\nПодробности: [[{s['note']}]]"
                    out.append(Reminder(rcpt, emp["id"], s["id"], kind, text))
                    if mark:
                        sent[key] = today.isoformat()
        if mark and out:
            store.save()
    return out


# ---------------------------------------------------------------- метрики

def metrics(store: Store, today: date, trace: Path = TRACE) -> dict:
    """Три метрики успеха: доступы, вопросы без HR, этапы в срок."""
    due_total = on_time = 0
    access_days = []
    for emp in store.employees.values():
        for s in store.route(emp):
            if not s.get("mandatory", True) or due_date(emp, s) > today:
                continue
            due_total += 1
            on_time += stage_status(emp, s, today) == "done"
        acc = emp["done"].get("access_standard")
        if acc:
            access_days.append((date.fromisoformat(acc["at"]) - date.fromisoformat(emp["start_date"])).days)
    answered = total = 0
    if trace.exists():
        for line in trace.read_text(encoding="utf-8").splitlines():
            try:
                route = json.loads(line)["route"]
            except (ValueError, KeyError):
                continue
            total += 1
            answered += route == "ANSWER"
    pct = lambda a, b: round(100 * a / b, 1) if b else None
    return {
        "access_days_vs_start": round(sum(access_days) / len(access_days), 1) if access_days else None,
        "questions_without_hr_pct": pct(answered, total),
        "questions_total": total,
        "mandatory_on_time_pct": pct(on_time, due_total),
        "mandatory_due": due_total,
    }


def format_metrics(m: dict) -> str:
    acc = m["access_days_vs_start"]
    acc_s = "нет данных" if acc is None else (f"{abs(acc)} дн. {'до' if acc <= 0 else 'после'} выхода")
    fmt = lambda v: "нет данных" if v is None else f"{v}%"
    return (f"📊 Метрики онбординга\n"
            f"• Стандартные доступы выданы в среднем: {acc_s}\n"
            f"• Вопросов закрыто без HR: {fmt(m['questions_without_hr_pct'])} (всего {m['questions_total']})\n"
            f"• Обязательных этапов в срок: {fmt(m['mandatory_on_time_pct'])} (из {m['mandatory_due']} наступивших)")


def format_status(store: Store, today: date) -> str:
    lines = [f"👥 Онбординг на {today:%d.%m.%Y}"]
    for emp in store.employees.values():
        sts = [stage_status(emp, s, today) for s in store.route(emp)]
        done = sum(x in ("done", "late_done") for x in sts)
        day = (today - date.fromisoformat(emp["start_date"])).days
        lines.append(f"• {emp['name']} ({emp['id']}), день {day:+d}: готово {done}/{len(sts)}"
                     + (f", ❗ просрочено {sts.count('overdue')}" if "overdue" in sts else ""))
    return "\n".join(lines)


# ---------------------------------------------------------------- CLI

def _cli():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", type=date.fromisoformat, default=date.today(), help="«сегодня» для проверки, YYYY-MM-DD")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    sub.add_parser("metrics")
    sh = sub.add_parser("show")
    sh.add_argument("emp")
    rm = sub.add_parser("remind")
    rm.add_argument("--dry-run", action="store_true", help="не запоминать отправленное")
    cf = sub.add_parser("confirm")
    cf.add_argument("emp")
    cf.add_argument("stage")
    cf.add_argument("--role", default="hr", choices=sorted(STAFF_ROLES | {"сотрудник"}))
    cf.add_argument("--by", required=True)
    ad = sub.add_parser("add")
    for a in ("id", "name", "position", "department", "start", "manager"):
        ad.add_argument(f"--{a}", required=True)
    ad.add_argument("--tg", default="")
    ad.add_argument("--manager-tg", default="")
    args = ap.parse_args()

    store = Store()
    try:
        if args.cmd == "status":
            print(format_status(store, args.date))
        elif args.cmd == "show":
            print(format_route(store, store.get(args.emp), args.date))
        elif args.cmd == "remind":
            rems = due_reminders(store, args.date, mark=not args.dry_run)
            for r in rems:
                print(f"→ {r.to:<8} [{r.emp_id}/{r.stage_id}] {r.text}")
            print(f"\nНапоминаний: {len(rems)}")
        elif args.cmd == "confirm":
            st = store.confirm(args.emp, args.stage, args.role, args.by, args.date)
            print(f"✅ {args.emp}: «{st['title']}» отмечен ({args.role}, {args.by})")
        elif args.cmd == "add":
            store.add({"id": args.id, "name": args.name, "position": args.position, "department": args.department,
                       "start_date": args.start, "manager": args.manager, "tg": args.tg.lstrip("@"),
                       "manager_tg": args.manager_tg.lstrip("@")})
            print(format_route(store, store.get(args.id), args.date))
        elif args.cmd == "metrics":
            print(format_metrics(metrics(store, args.date)))
    except (KeyError, ValueError, PermissionError) as e:
        sys.exit(f"✖ {e}")


if __name__ == "__main__":
    _cli()
