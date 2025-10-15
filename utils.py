def generate_caption(title: str, description: str) -> str:
    title = (title or "").strip()
    description = (description or "").strip()
    base = f"{title} — {description}"[:160]
    if not base:
        return "Домашний хит от CookNet AI 😋"
    return base
