"""
tools/subscription.py — a única fonte de verdade sobre "esse usuário pode?".

Módulo PURO: não fala com banco nem com HTTP. Recebe os dados já prontos
(a linha do usuário e, quando preciso, a contagem do mês ou o tamanho do
período) e devolve um Verdict. Isso o deixa fácil de testar isolado.

Quem chama decide o que fazer com o Verdict:
- paywall LIGADO   -> bloqueia de verdade quando `liberado` é False
- paywall DESLIGADO (modo sombra) -> só registra no log o que TERIA bloqueado
"""

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone

# --- Regras do plano free ---------------------------------------------------
LIMITE_LANCAMENTOS_FREE = 20      # lançamentos por mês no free
LIMITE_DIAS_RELATORIO_FREE = 7    # janela máxima de relatório no free

# --- Ações que passam pelo gate ---------------------------------------------
ACAO_REGISTRAR = "registrar"
ACAO_RELATORIO = "relatorio"

# --- Motivos de bloqueio (o paywall usa pra escolher a mensagem) ------------
MOTIVO_COTA = "cota_estourada"
MOTIVO_PERIODO = "periodo_longo"
MOTIVO_EXPIRADO = "plano_expirado"


@dataclass
class Verdict:
    liberado: bool
    motivo: str | None = None                  # None quando liberado
    meta: dict = field(default_factory=dict)   # extras p/ a mensagem (usados/limite, dias...)


def _admin_ids() -> set[int]:
    """Lê ADMIN_TELEGRAM_IDS (ex.: '123,456') e devolve um set de ints."""
    ids = set()
    for parte in os.environ.get("ADMIN_TELEGRAM_IDS", "").split(","):
        parte = parte.strip()
        if parte.isdigit():
            ids.add(int(parte))
    return ids


def paywall_ativo() -> bool:
    """
    PAYWALL_ENABLED controla se o gate BLOQUEIA de verdade.
    Ausente ou 'false'/'0'/'no' => desligado (modo sombra).
    Quem enforça usa isto; o check_access NÃO usa, de propósito, pra
    conseguir calcular o veredito mesmo no modo sombra.
    """
    return os.environ.get("PAYWALL_ENABLED", "").strip().lower() in ("true", "1", "yes", "sim")


def _para_datetime(valor) -> datetime | None:
    """Aceita string ISO (do Supabase) ou datetime; devolve datetime tz-aware (UTC)."""
    if valor is None:
        return None
    dt = valor if isinstance(valor, datetime) else None
    if dt is None:
        try:
            dt = datetime.fromisoformat(str(valor).replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def check_access(
    user_row: dict,
    acao: str,
    *,
    lancamentos_mes: int = 0,
    periodo_dias: int = 0,
    agora: datetime | None = None,
) -> Verdict:
    """
    Decide se o usuário pode fazer `acao`. NÃO olha o PAYWALL_ENABLED
    (isso é enforcement, fica com quem chama). Ordem de avaliação:

      1. admin            -> liberado (vale até em produção)
      2. vitalício        -> liberado
      3. plano pago válido (subscription_expires_at > agora) -> liberado
      4. plano pago vencido (plus_* e já passou) -> BLOQUEADO (plano_expirado)
      5. free/registrar   -> liberado se lançamentos do mês < 20
         free/relatorio   -> liberado se período <= 7 dias
      6. senão            -> BLOQUEADO com o motivo
    """
    agora = agora or datetime.now(timezone.utc)
    telegram_id = user_row.get("telegram_id")
    plano = user_row.get("plan_type") or "free"

    # 1) admin nunca é bloqueado
    if telegram_id in _admin_ids():
        return Verdict(True, meta={"admin": True})

    # 2) vitalício
    if plano == "lifetime":
        return Verdict(True)

    # 3) plano pago dentro da validade
    expira = _para_datetime(user_row.get("subscription_expires_at"))
    if expira is not None and expira > agora:
        return Verdict(True)

    # 4) plano pago que venceu (ainda marcado plus_*, mas expirou)
    if plano in ("plus_monthly", "plus_annual"):
        return Verdict(False, MOTIVO_EXPIRADO)

    # 5) free
    if acao == ACAO_REGISTRAR:
        meta = {"usados": lancamentos_mes, "limite": LIMITE_LANCAMENTOS_FREE}
        return Verdict(lancamentos_mes < LIMITE_LANCAMENTOS_FREE,
                       None if lancamentos_mes < LIMITE_LANCAMENTOS_FREE else MOTIVO_COTA,
                       meta=meta)

    if acao == ACAO_RELATORIO:
        meta = {"dias": periodo_dias, "limite": LIMITE_DIAS_RELATORIO_FREE}
        return Verdict(periodo_dias <= LIMITE_DIAS_RELATORIO_FREE,
                       None if periodo_dias <= LIMITE_DIAS_RELATORIO_FREE else MOTIVO_PERIODO,
                       meta=meta)

    # 6) ação desconhecida: não travar um fluxo novo por engano
    return Verdict(True)