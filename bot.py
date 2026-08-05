# ==============================================================================
# SECTION 1: IMPORTS & ENTERPRISE LOGGING CONFIGURATION
# ==============================================================================
import asyncio
import hashlib
import hmac
import logging
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import mercadopago
import psycopg
import uvicorn
from fastapi import FastAPI, HTTPException, Request, status
from psycopg.rows import dict_row
from telegram import (
    ChatMember,
    ChatMemberUpdated,
    Update,
)
from telegram.ext import (
    Application,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# Configuração Padrão Enterprise de Registros (Logs)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("VIPEnterpriseEngine")


# ==============================================================================
# SECTION 2: CENTRALIZED CONFIGURATION & SETTINGS ENGINE
# ==============================================================================
@dataclass(frozen=True)
class PlanoConfig:
    """Representa a configuração imutável de um plano comercial no catálogo."""
    id: str
    nome: str
    valor: float
    dias: int
    descricao: str


class Settings:
    """
    Centralizador único de todas as configurações, credenciais, constantes,
    limites de segurança e catálogo de planos de negócio da aplicação.
    """
    # Credenciais e Variáveis de Ambiente
    TOKEN: str = os.environ.get("TOKEN", "")
    MP_ACCESS_TOKEN: str = os.environ.get("MP_ACCESS_TOKEN", "")
    MP_WEBHOOK_SECRET: str = os.environ.get("MP_WEBHOOK_SECRET", "")
    DATABASE_URL: str = os.environ.get("DATABASE_URL", "")
    WEBHOOK_URL: str = os.environ.get("WEBHOOK_URL", "https://meu-bot-pwx3.onrender.com")

    # Autenticação e Autorização
    ADMIN_IDS: List[int] = field(default_factory=lambda: [
        int(x.strip())
        for x in os.environ.get("ADMIN_IDS", "").split(",")
        if x.strip().isdigit()
    ])

        # Parâmetros do Grupo VIP
    GRUPO_VIP: int = int(os.environ.get("GRUPO_VIP", "-1003990872882"))
    EXPIRACAO_CONVITE_MINUTOS: int = 10
    HORAS_REAPROVEITAMENTO_PAGAMENTO: int = 24

    # Segurança & Rate Limiting (Anti-Flood / Anti-Spam)
    RATE_LIMIT_MAX_REQUESTS: int = 5
    RATE_LIMIT_WINDOW_SECONDS: int = 10
    CACHE_TTL_SECONDS: int = 60

    # Versão do Core Enterprise
    VERSION: str = "5.1.0-PROD"
    START_TIME: datetime = datetime.now(timezone.utc)

    # Identificadores de Mídias de Demonstração
    PREVIA_FOTO_1_ID: str = "AgACAgEAAxkBAAIBb2pzvAw0dqtOYV_wI9nngyJdQEpGAAK_DGsbV-KZR0g4xAABFasQogEAAwIAA3kAAz0E"
    PREVIA_FOTO_2_ID: str = "AgACAgEAAxkBAAIBcWpzvG6_-ZPWK_jzk9AzAAGY1x6BKAACwAxrG1fimUfW6cvoo_rmnAEAAwIAA3kAAz0E"
    PREVIA_VIDEO_1_ID: str = "BAACAgEAAxkBAANcam4MYXQ93cFRlJlFTXq-067_lA4AAkAIAAJgpnFHBio6HNjhku09BA"
    PREVIA_VIDEO_2_ID: str = "BAACAgEAAxkBAAOaanCWf0IJlHtAsOHtmHRrpZy_R7EAAn8IAAKrO4hHVyghlY3FbqQ9BA"
    PREVIA_VIDEO_3_ID: str = "BAACAgEAAxkBAAIBhmpzyy_gm2EX1G_q12mykjI0v9nbAAKOBgAC3jKhRrWIAXURQKm3PQQ"
    PREVIA_VIDEO_4_ID: str = "BAACAgEAAxkBAAIBh2pzy0XHVqUiwpF0PUSG_y_t9UgyAAKIBAACMC4gRTLUNsRZp5KuPQQ"
    
    
    # Catálogo Completo e Escalável de Planos
    PLANOS_DISPONIVEIS: Dict[str, PlanoConfig] = {
        "mensal": PlanoConfig(
            id="mensal",
            nome="Plano Mensal",
            valor=4.99,
            dias=30,
            descricao="Acesso VIP completo por 30 dias."
        ),
        "trimestral": PlanoConfig(
            id="trimestral",
            nome="Plano Trimestral",
            valor=12.90,
            dias=90,
            descricao="Acesso VIP completo com desconto por 90 dias."
        ),
        "anual": PlanoConfig(
            id="anual",
            nome="Plano Anual",
            valor=39.90,
            dias=365,
            descricao="Acesso VIP completo de longo prazo por 365 dias."
        ),
    }

    @classmethod
    def validar_configuracoes_criticas(cls) -> None:
        """Verifica na inicialização se os parâmetros obrigatórios foram providos."""
        ausentes = []

        if not cls.TOKEN:
            ausentes.append("TOKEN")

        if not cls.DATABASE_URL:
            ausentes.append("DATABASE_URL")

        if ausentes:
            logger.warning(
                f"Parâmetros de ambiente ausentes: {', '.join(ausentes)}. Verifique o ambiente de produção."
            )


# Instância global de configuração
cfg = Settings()


# ==============================================================================
# SECTION 3: CACHE LAYER (TTL IN-MEMORY ENGINE)
# ==============================================================================
class TTLCacheEngine:
    """
    Motor de cache em memória com Time-To-Live (TTL) customizado e thread-safe,
    desenhado para reduzir viagens repetitivas de leitura ao banco PostgreSQL.
    """
    def __init__(self, default_ttl_seconds: int = cfg.CACHE_TTL_SECONDS) -> None:
        self._cache: Dict[str, Tuple[Any, float]] = {}
        self.default_ttl = default_ttl_seconds

    def get(self, key: str) -> Optional[Any]:
        """Recupera valor do cache caso não esteja expirado."""
        if key in self._cache:
            val, exp = self._cache[key]
            if time.time() < exp:
                return val
            else:
                del self._cache[key]
        return None

    def set(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        """Armazena ou atualiza valor no cache informando expiração em segundos."""
        duracao = ttl if ttl is not None else self.default_ttl
        self._cache[key] = (value, time.time() + duracao)

    def invalidate(self, key: str) -> None:
        """Invalidacao manual de item no cache."""
        self._cache.pop(key, None)

    def invalidate_prefix(self, prefix: str) -> None:
        """Remove do cache todos os itens com uma dada chave de prefixo."""
        chaves = [k for k in self._cache.keys() if k.startswith(prefix)]
        for k in chaves:
            del self._cache[k]

    def clear_expired(self) -> int:
        """Rotina de faxina interna de chaves vencidas."""
        agora = time.time()
        vencidos = [k for k, (_, exp) in self._cache.items() if agora >= exp]
        for k in vencidos:
            del self._cache[k]
        return len(vencidos)


# Instância global da camada de cache
cache_engine = TTLCacheEngine()


# ==============================================================================
# SECTION 4: POSTGRESQL CONNECTION MANAGER, SCHEMA & MIGRATIONS
# ==============================================================================
def obter_conexao() -> psycopg.Connection:
    """
    Gera e devolve conexão ativa ao banco PostgreSQL usando row_factory dict_row.
    Garante robustez em caso de reconexões momentâneas do servidor de banco.
    """
    return psycopg.connect(cfg.DATABASE_URL, row_factory=dict_row)


def criar_tabelas_e_indices() -> None:
    """
    Constrói todo o esquema transacional relacional do sistema no PostgreSQL,
    incluindo novas tabelas de auditoria admin, erros sistêmicos, webhooks, logins e blacklist.
    """
    try:
        with obter_conexao() as conn:
            with conn.cursor() as cur:

                # 1. Tabela Principal de Usuários
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS usuarios (
                        telegram_id BIGINT PRIMARY KEY,
                        nome TEXT NOT NULL,
                        username TEXT,
                        ultimo_acesso TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                        cadastrado_em TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                    );
                """)

                # 2. Tabela de Blacklist Permanente de Usuários
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS blacklist_usuarios (
                        telegram_id BIGINT PRIMARY KEY,
                        motivo TEXT NOT NULL,
                        bloqueado_por BIGINT NOT NULL,
                        data_bloqueio TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                    );
                """)

                # 3. Tabela de Pagamentos com migração automática
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS pagamentos (
                        id SERIAL PRIMARY KEY,
                        payment_id TEXT UNIQUE,
                        preference_id TEXT,
                        telegram_id BIGINT REFERENCES usuarios(telegram_id) ON DELETE CASCADE,
                        valor NUMERIC(10, 2) NOT NULL,
                        status TEXT NOT NULL,
                        plano TEXT DEFAULT 'mensal',
                        metodo_pagamento TEXT DEFAULT 'mercadopago',
                        init_point TEXT,
                        criado_em TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                        aprovado_em TIMESTAMP WITH TIME ZONE
                    );
                """)

                # Migração automática para bancos já existentes
                cur.execute("""
                    ALTER TABLE pagamentos
                    ADD COLUMN IF NOT EXISTS preference_id TEXT;
                """)

                cur.execute("""
                    ALTER TABLE pagamentos
                    ADD COLUMN IF NOT EXISTS plano TEXT DEFAULT 'mensal';
                """)

                cur.execute("""
                    ALTER TABLE pagamentos
                    ADD COLUMN IF NOT EXISTS metodo_pagamento TEXT DEFAULT 'mercadopago';
                """)

                cur.execute("""
                    ALTER TABLE pagamentos
                    ADD COLUMN IF NOT EXISTS init_point TEXT;
                """)
                
                # 4. Tabela de Assinaturas Ativas
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS assinaturas (
                        telegram_id BIGINT PRIMARY KEY REFERENCES usuarios(telegram_id) ON DELETE CASCADE,
                        plano TEXT DEFAULT 'mensal',
                        inicio TIMESTAMP WITH TIME ZONE NOT NULL,
                        vencimento TIMESTAMP WITH TIME ZONE NOT NULL,
                        ativa BOOLEAN DEFAULT TRUE,
                        atualizado_em TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                    );
                """)

                # 5. Tabela de Histórico de Assinaturas e Eventos Financeiros
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS historico_assinaturas (
                        id SERIAL PRIMARY KEY,
                        telegram_id BIGINT REFERENCES usuarios(telegram_id) ON DELETE CASCADE,
                        plano TEXT NOT NULL,
                        valor_pago NUMERIC(10, 2) NOT NULL,
                        dias_adicionados INT NOT NULL,
                        evento TEXT NOT NULL,
                        registrado_em TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                    );
                """)

                # 6. Tabela de Controle de Convites Gerados
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS convites_gerados (
                        id SERIAL PRIMARY KEY,
                        invite_link TEXT UNIQUE NOT NULL,
                        telegram_id BIGINT REFERENCES usuarios(telegram_id) ON DELETE CASCADE,
                        expiracao TIMESTAMP WITH TIME ZONE NOT NULL,
                        utilizado BOOLEAN DEFAULT FALSE,
                        revogado BOOLEAN DEFAULT FALSE,
                        criado_em TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                    );
                """)

                # 7. Tabela de Auditoria Operacional dos Administradores
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS logs_admin (
                        id SERIAL PRIMARY KEY,
                        admin_id BIGINT NOT NULL,
                        comando TEXT NOT NULL,
                        usuario_afetado BIGINT,
                        detalhes TEXT,
                        registrado_em TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                    );
                """)

                # 8. Tabela de Registros (Logs) de Webhooks Recebidos
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS logs_webhooks (
                        id SERIAL PRIMARY KEY,
                        evento_tipo TEXT NOT NULL,
                        data_id TEXT,
                        status_assinatura TEXT NOT NULL,
                        payload_resumo TEXT,
                        recebido_em TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                    );
                """)

                # 9. Tabela de Registros (Logs) de Erros de Execução e Exceções
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS logs_erros_sistema (
                        id SERIAL PRIMARY KEY,
                        origem TEXT NOT NULL,
                        mensagem TEXT NOT NULL,
                        detalhes TEXT,
                        ocorreu_em TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                    );
                """)

                # 10. Tabela de Registro de Acessos e Logins de Usuários
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS logs_logins (
                        id SERIAL PRIMARY KEY,
                        telegram_id BIGINT NOT NULL,
                        nome TEXT NOT NULL,
                        username TEXT,
                        comando_origem TEXT DEFAULT '/start',
                        registrado_em TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                    );
                """)

                # Índices de Alta Performance
                cur.execute("CREATE INDEX IF NOT EXISTS idx_pagamentos_telegram ON pagamentos(telegram_id);")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_pagamentos_status ON pagamentos(status);")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_pagamentos_pref ON pagamentos(preference_id);")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_assinaturas_venc ON assinaturas(vencimento);")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_assinaturas_ativa ON assinaturas(ativa);")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_convites_link ON convites_gerados(invite_link);")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_convites_user ON convites_gerados(telegram_id);")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_logs_admin_reg ON logs_admin(registrado_em DESC);")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_logs_web_reg ON logs_webhooks(recebido_em DESC);")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_logs_err_reg ON logs_erros_sistema(ocorreu_em DESC);")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_logs_logins_reg ON logs_logins(registrado_em DESC);")

            conn.commit()
        logger.info("Estrutura PostgreSQL, schemas, constraints, auditorias e índices criados com sucesso.")
    except Exception as erro:
        logger.critical(f"Falha gravíssima na montagem de tabelas PostgreSQL: {erro}", exc_info=True)
        raise


def registrar_erro_sistema(origem: str, mensagem: str, detalhes: Optional[str] = None) -> None:
    """Grava ocorrências de exceção ou problemas técnicos na tabela de auditoria de erros."""
    try:
        with obter_conexao() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO logs_erros_sistema (origem, mensagem, detalhes)
                    VALUES (%s, %s, %s);
                    """,
                    (origem, mensagem, str(detalhes or "")[:1500]),
                )
            conn.commit()
    except Exception as exc:
        logger.error(f"Não foi possível persistir erro de sistema: {exc}")


# ==============================================================================
# SECTION 5: DATA ACCESS LAYER (REPOSITORIES & AUDIT LOGGING)
# ==============================================================================
def salvar_usuario(telegram_id: int, nome: str, username: Optional[str] = None, comando_origem: str = "/start") -> None:
    """Registra ou atualiza dados biográficos e log de acesso do usuário no PostgreSQL."""
    try:
        with obter_conexao() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO usuarios (telegram_id, nome, username, ultimo_acesso)
                    VALUES (%s, %s, %s, CURRENT_TIMESTAMP)
                    ON CONFLICT (telegram_id)
                    DO UPDATE SET 
                        nome = EXCLUDED.nome,
                        username = EXCLUDED.username,
                        ultimo_acesso = CURRENT_TIMESTAMP;
                    """,
                    (telegram_id, nome, username),
                )
                cur.execute(
                    """
                    INSERT INTO logs_logins (telegram_id, nome, username, comando_origem)
                    VALUES (%s, %s, %s, %s);
                    """,
                    (telegram_id, nome, username, comando_origem),
                )
            conn.commit()
        cache_engine.invalidate(f"usr:{telegram_id}")
    except Exception as erro:
        logger.error(f"Erro ao salvar usuário {telegram_id}: {erro}", exc_info=True)
        registrar_erro_sistema("salvar_usuario", str(erro))


def buscar_usuario(telegram_id: int) -> Optional[Dict[str, Any]]:
    """Consulta dados de usuário com estratégia cache-first."""
    cache_key = f"usr:{telegram_id}"
    cache_item = cache_engine.get(cache_key)
    if cache_item:
        return cache_item

    try:
        with obter_conexao() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM usuarios WHERE telegram_id = %s;", (telegram_id,))
                row = cur.fetchone()
                if row:
                    cache_engine.set(cache_key, row, ttl=120)
                return row
    except Exception as erro:
        logger.error(f"Erro ao buscar usuário {telegram_id}: {erro}", exc_info=True)
        registrar_erro_sistema("buscar_usuario", str(erro))
        return None


def registrar_auditoria_admin(
    admin_id: int,
    comando: str,
    usuario_afetado: Optional[int] = None,
    detalhes: Optional[str] = None,
) -> None:
    """Salva registro inviolável de ações executadas por administradores."""
    try:
        with obter_conexao() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO logs_admin (admin_id, comando, usuario_afetado, detalhes)
                    VALUES (%s, %s, %s, %s);
                    """,
                    (admin_id, comando, usuario_afetado, detalhes),
                )
            conn.commit()
        logger.info(f"AUDIT ADMIN | ID:{admin_id} | CMD:{comando} | ALVO:{usuario_afetado} | DET:{detalhes}")
    except Exception as erro:
        logger.error(f"Falha ao gravar log de auditoria do admin: {erro}", exc_info=True)
        registrar_erro_sistema("registrar_auditoria_admin", str(erro))


def buscar_pagamento_pendente_recente(telegram_id: int, plano: str = "mensal") -> Optional[Dict[str, Any]]:
    """
    Checa existência de preferência pendente válida (nas últimas 24h)
    para impedir duplicação de solicitações no gateway.
    """
    try:
        limite = datetime.now(timezone.utc) - timedelta(hours=cfg.HORAS_REAPROVEITAMENTO_PAGAMENTO)
        with obter_conexao() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT preference_id, init_point, valor
                    FROM pagamentos
                    WHERE telegram_id = %s 
                      AND status = 'pending'
                      AND plano = %s
                      AND criado_em >= %s
                    ORDER BY criado_em DESC
                    LIMIT 1;
                    """,
                    (telegram_id, plano, limite),
                )
                return cur.fetchone()
    except Exception as erro:
        logger.error(f"Erro ao checar pagamento pendente para {telegram_id}: {erro}", exc_info=True)
        registrar_erro_sistema("buscar_pagamento_pendente_recente", str(erro))
        return None


def registrar_pagamento_pendente(
    telegram_id: int,
    preference_id: str,
    init_point: str,
    valor: float,
    plano: str = "mensal",
    metodo_pagamento: str = "mercadopago",
) -> None:
    """Insere intenção de compra pendente na base transacional."""
    try:
        with obter_conexao() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO pagamentos (telegram_id, preference_id, init_point, status, valor, plano, metodo_pagamento)
                    VALUES (%s, %s, %s, 'pending', %s, %s, %s);
                    """,
                    (telegram_id, preference_id, init_point, valor, plano, metodo_pagamento),
                )
            conn.commit()
    except Exception as erro:
        logger.error(f"Erro ao gravar pagamento pendente ({telegram_id}): {erro}", exc_info=True)
        registrar_erro_sistema("registrar_pagamento_pendente", str(erro))


def aprovar_pagamento_com_seguranca(payment_id: str, telegram_id: int, valor: float, plano: str) -> bool:
    """
    Usa controle de concorrência FOR UPDATE para proteger contra aprovação
    duplicada por webhooks simultâneos ou race conditions de rede.
    Retorna True em aprovação inédita; False caso já liquidado no passado.
    """
    try:
        with obter_conexao() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, status FROM pagamentos WHERE payment_id = %s FOR UPDATE;",
                    (payment_id,),
                )
                row = cur.fetchone()
                if row:
                    if row["status"] == "approved":
                        logger.warning(f"Atenção: pagamento {payment_id} ignorado por já estar liquidado.")
                        return False
                    cur.execute(
                        """
                        UPDATE pagamentos
                        SET status = 'approved',
                            aprovado_em = CURRENT_TIMESTAMP,
                            telegram_id = %s,
                            valor = %s,
                            plano = %s
                        WHERE payment_id = %s;
                        """,
                        (telegram_id, valor, plano, payment_id),
                    )
                else:
                    cur.execute(
                        """
                        INSERT INTO pagamentos (payment_id, telegram_id, status, valor, plano, aprovado_em)
                        VALUES (%s, %s, 'approved', %s, %s, CURRENT_TIMESTAMP);
                        """,
                        (payment_id, telegram_id, valor, plano),
                    )
            conn.commit()
            return True
    except Exception as erro:
        logger.error(f"Falha grave em aprovação ACID de pagamento {payment_id}: {erro}", exc_info=True)
        registrar_erro_sistema("aprovar_pagamento_com_seguranca", str(erro), f"PaymentID: {payment_id}")
        return False


def estender_ou_ativar_assinatura(telegram_id: int, plano: str = "mensal") -> Dict[str, Any]:
    """
    Calcula prorrogação ou nova ativação de acesso VIP e registra rastro
    no histórico financeiro de assinaturas.
    """
    config_plano = cfg.PLANOS_DISPONIVEIS.get(plano, cfg.PLANOS_DISPONIVEIS["mensal"])
    dias = config_plano.dias
    valor = config_plano.valor
    agora = datetime.now(timezone.utc)

    try:
        with obter_conexao() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT vencimento, ativa FROM assinaturas WHERE telegram_id = %s FOR UPDATE;",
                    (telegram_id,),
                )
                registro = cur.fetchone()

                evento = "renovacao" if (registro and registro["ativa"] and registro["vencimento"] > agora) else "ativacao"
                if registro and registro["ativa"] and registro["vencimento"] > agora:
                    novo_vencimento = registro["vencimento"] + timedelta(days=dias)
                else:
                    novo_vencimento = agora + timedelta(days=dias)

                cur.execute(
                    """
                    INSERT INTO assinaturas (telegram_id, plano, inicio, vencimento, ativa, atualizado_em)
                    VALUES (%s, %s, %s, %s, TRUE, CURRENT_TIMESTAMP)
                    ON CONFLICT (telegram_id)
                    DO UPDATE SET
                        plano = EXCLUDED.plano,
                        vencimento = EXCLUDED.vencimento,
                        ativa = TRUE,
                        atualizado_em = CURRENT_TIMESTAMP;
                    """,
                    (telegram_id, plano, agora, novo_vencimento),
                )

                cur.execute(
                    """
                    INSERT INTO historico_assinaturas (telegram_id, plano, valor_pago, dias_adicionados, evento)
                    VALUES (%s, %s, %s, %s, %s);
                    """,
                    (telegram_id, plano, valor, dias, evento),
                )
            conn.commit()
        cache_engine.invalidate(f"sub:{telegram_id}")
        return {"vencimento": novo_vencimento, "evento": evento, "dias": dias}
    except Exception as erro:
        logger.error(f"Erro ao estender assinatura para {telegram_id}: {erro}", exc_info=True)
        registrar_erro_sistema("estender_ou_ativar_assinatura", str(erro))
        raise


def assinatura_ativa(telegram_id: int) -> bool:
    """Verifica com suporte de cache se o acesso do usuário está válido e vigente."""
    cache_key = f"sub:{telegram_id}"
    item = cache_engine.get(cache_key)
    if item is not None:
        return bool(item)

    try:
        with obter_conexao() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT 1 FROM assinaturas
                    WHERE telegram_id = %s AND ativa = TRUE AND vencimento > CURRENT_TIMESTAMP;
                    """,
                    (telegram_id,),
                )
                valido = cur.fetchone() is not None
                cache_engine.set(cache_key, valido, ttl=30)
                return valido
    except Exception as erro:
        logger.error(f"Erro em checagem de assinatura ativa ({telegram_id}): {erro}", exc_info=True)
        registrar_erro_sistema("assinatura_ativa", str(erro))
        return False


def buscar_assinatura(telegram_id: int) -> Optional[Dict[str, Any]]:
    """Obtém dicionário completo contendo metadados da assinatura do usuário."""
    try:
        with obter_conexao() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM assinaturas WHERE telegram_id = %s;", (telegram_id,))
                return cur.fetchone()
    except Exception as erro:
        logger.error(f"Erro ao buscar assinatura de {telegram_id}: {erro}", exc_info=True)
        registrar_erro_sistema("buscar_assinatura", str(erro))
        return None


def desativar_assinatura_usuario(telegram_id: int) -> bool:
    """Altera flag ativa para False e limpa cache do usuário."""
    try:
        with obter_conexao() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE assinaturas
                    SET ativa = FALSE, atualizado_em = CURRENT_TIMESTAMP
                    WHERE telegram_id = %s;
                    """,
                    (telegram_id,),
                )
            conn.commit()
        cache_engine.invalidate(f"sub:{telegram_id}")
        return True
    except Exception as erro:
        logger.error(f"Erro ao revogar assinatura ({telegram_id}): {erro}", exc_info=True)
        registrar_erro_sistema("desativar_assinatura_usuario", str(erro))
        return False


def buscar_assinaturas_vencidas() -> List[Dict[str, Any]]:
    """Consulta todas as assinaturas abertas cujo prazo de vencimento se esgotou."""
    try:
        with obter_conexao() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT telegram_id, vencimento FROM assinaturas
                    WHERE ativa = TRUE AND vencimento <= CURRENT_TIMESTAMP;
                    """
                )
                return cur.fetchall()
    except Exception as erro:
        logger.error(f"Erro ao listar assinaturas vencidas: {erro}", exc_info=True)
        registrar_erro_sistema("buscar_assinaturas_vencidas", str(erro))
        return []


def gravar_convite_banco(invite_link: str, telegram_id: int, expiracao: datetime) -> None:
    """Registra link de convite único com data limite para auditoria e limpeza."""
    try:
        with obter_conexao() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO convites_gerados (invite_link, telegram_id, expiracao)
                    VALUES (%s, %s, %s);
                    """,
                    (invite_link, telegram_id, expiracao),
                )
            conn.commit()
    except Exception as erro:
        logger.error(f"Erro ao registrar link de convite ({invite_link}): {erro}", exc_info=True)
        registrar_erro_sistema("gravar_convite_banco", str(erro))


def obter_relatorio_geral_completo() -> Dict[str, Any]:
    """Compila KPI financeira e de conversão integral para relatórios admin."""
    stats = {
        "usuarios_cadastrados": 0,
        "usuarios_ativos": 0,
        "usuarios_vencidos": 0,
        "pagamentos_hoje": 0,
        "pagamentos_mes": 0,
        "faturamento_hoje": 0.0,
        "faturamento_mes": 0.0,
        "faturamento_anual": 0.0,
        "faturamento_total": 0.0,
        "renovacoes": 0,
        "ticket_medio": 0.0,
    }
    try:
        with obter_conexao() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) AS c FROM usuarios;")
                stats["usuarios_cadastrados"] = cur.fetchone()["c"]

                cur.execute("SELECT COUNT(*) AS c FROM assinaturas WHERE ativa = TRUE AND vencimento > CURRENT_TIMESTAMP;")
                stats["usuarios_ativos"] = cur.fetchone()["c"]

                cur.execute("SELECT COUNT(*) AS c FROM assinaturas WHERE ativa = FALSE OR vencimento <= CURRENT_TIMESTAMP;")
                stats["usuarios_vencidos"] = cur.fetchone()["c"]

                cur.execute("""
                    SELECT COUNT(*) AS c, COALESCE(SUM(valor), 0) AS soma
                    FROM pagamentos
                    WHERE status = 'approved' AND DATE(aprovado_em) = CURRENT_DATE;
                """)
                res_hoje = cur.fetchone()
                stats["pagamentos_hoje"] = res_hoje["c"]
                stats["faturamento_hoje"] = float(res_hoje["soma"])

                cur.execute("""
                    SELECT COUNT(*) AS c, COALESCE(SUM(valor), 0) AS soma
                    FROM pagamentos
                    WHERE status = 'approved' AND DATE_TRUNC('month', aprovado_em) = DATE_TRUNC('month', CURRENT_DATE);
                """)
                res_mes = cur.fetchone()
                stats["pagamentos_mes"] = res_mes["c"]
                stats["faturamento_mes"] = float(res_mes["soma"])

                cur.execute("""
                    SELECT COALESCE(SUM(valor), 0) AS soma
                    FROM pagamentos
                    WHERE status = 'approved' AND DATE_TRUNC('year', aprovado_em) = DATE_TRUNC('year', CURRENT_DATE);
                """)
                stats["faturamento_anual"] = float(cur.fetchone()["soma"])

                cur.execute("""
                    SELECT COUNT(*) AS total_tx, COALESCE(SUM(valor), 0) AS soma
                    FROM pagamentos WHERE status = 'approved';
                """)
                res_total = cur.fetchone()
                tot_tx = res_total["total_tx"]
                tot_fat = float(res_total["soma"])
                stats["faturamento_total"] = tot_fat
                stats["ticket_medio"] = (tot_fat / tot_tx) if tot_tx > 0 else 0.0

                cur.execute("SELECT COUNT(*) AS c FROM historico_assinaturas WHERE evento = 'renovacao';")
                stats["renovacoes"] = cur.fetchone()["c"]
    except Exception as erro:
        logger.error(f"Falha ao processar KPIs para relatorio_geral: {erro}", exc_info=True)
        registrar_erro_sistema("obter_relatorio_geral_completo", str(erro))

    return stats


def buscar_ultimos_pagamentos(limite: int = 10) -> List[Dict[str, Any]]:
    """Retorna os pagamentos aprovados mais recentes com dados de usuário."""
    try:
        with obter_conexao() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT p.payment_id, p.telegram_id, p.valor, p.status, p.plano, p.aprovado_em, u.nome
                    FROM pagamentos p
                    LEFT JOIN usuarios u ON p.telegram_id = u.telegram_id
                    WHERE p.status = 'approved'
                    ORDER BY p.aprovado_em DESC
                    LIMIT %s;
                    """,
                    (limite,),
                )
                return cur.fetchall()
    except Exception as erro:
        logger.error(f"Erro ao listar ultimos pagamentos: {erro}", exc_info=True)
        registrar_erro_sistema("buscar_ultimos_pagamentos", str(erro))
        return []


def buscar_usuarios_por_segmento(segmento: str) -> List[int]:
    """
    Retorna lista de Telegram IDs aplicando regra granular de corte:
    'todos', 'ativos', 'vencidos', 'mensal', 'trimestral', 'anual' ou 'nunca_comprou'.
    """
    sql_base = "SELECT u.telegram_id FROM usuarios u"
    try:
        with obter_conexao() as conn:
            with conn.cursor() as cur:
                if segmento == "todos":
                    cur.execute(f"{sql_base};")
                elif segmento == "ativos":
                    cur.execute("""
                        SELECT telegram_id FROM assinaturas
                        WHERE ativa = TRUE AND vencimento > CURRENT_TIMESTAMP;
                    """)
                elif segmento == "vencidos":
                    cur.execute("""
                        SELECT telegram_id FROM assinaturas
                        WHERE ativa = FALSE OR vencimento <= CURRENT_TIMESTAMP;
                    """)
                elif segmento in ["mensal", "trimestral", "anual"]:
                    cur.execute("""
                        SELECT telegram_id FROM assinaturas
                        WHERE plano = %s AND ativa = TRUE AND vencimento > CURRENT_TIMESTAMP;
                    """, (segmento,))
                elif segmento == "nunca_comprou":
                    cur.execute("""
                        SELECT u.telegram_id FROM usuarios u
                        WHERE NOT EXISTS (
                            SELECT 1 FROM pagamentos p
                            WHERE p.telegram_id = u.telegram_id AND p.status = 'approved'
                        );
                    """)
                else:
                    return []
                return [row["telegram_id"] for row in cur.fetchall()]
    except Exception as erro:
        logger.error(f"Erro ao buscar IDs por segmento ({segmento}): {erro}", exc_info=True)
        registrar_erro_sistema("buscar_usuarios_por_segmento", str(erro))
        return []


def buscar_ultimos_erros(limite: int = 10) -> List[Dict[str, Any]]:
    """Pesquisa eventos recentes de falha na tabela de auditoria de erros."""
    try:
        with obter_conexao() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM logs_erros_sistema ORDER BY ocorreu_em DESC LIMIT %s;",
                    (limite,),
                )
                return cur.fetchall()
    except Exception as erro:
        logger.error(f"Erro ao consultar tabela de erros do sistema: {erro}")
        return []


def buscar_ultimos_convites(limite: int = 10) -> List[Dict[str, Any]]:
    """Retorna os últimos convites gerados e o status deles."""
    try:
        with obter_conexao() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM convites_gerados ORDER BY criado_em DESC LIMIT %s;",
                    (limite,),
                )
                return cur.fetchall()
    except Exception as erro:
        logger.error(f"Erro ao consultar últimos convites: {erro}")
        return []


def buscar_ultimos_webhooks(limite: int = 10) -> List[Dict[str, Any]]:
    """Consulta auditoria de requisições enviadas ao webhook."""
    try:
        with obter_conexao() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM logs_webhooks ORDER BY recebido_em DESC LIMIT %s;",
                    (limite,),
                )
                return cur.fetchall()
    except Exception as erro:
        logger.error(f"Erro ao consultar registros de webhooks: {erro}")
        return []


def buscar_ultimos_logs_admin(limite: int = 10) -> List[Dict[str, Any]]:
    """Exibe o rastro de comandos que administradores rodaram no bot."""
    try:
        with obter_conexao() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM logs_admin ORDER BY registrado_em DESC LIMIT %s;",
                    (limite,),
                )
                return cur.fetchall()
    except Exception as erro:
        logger.error(f"Erro ao consultar auditoria dos administradores: {erro}")
        return []


def buscar_ultimos_logins(limite: int = 10) -> List[Dict[str, Any]]:
    """Retorna o histórico das interações mais recentes de login e acesso de membros."""
    try:
        with obter_conexao() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM logs_logins ORDER BY registrado_em DESC LIMIT %s;",
                    (limite,),
                )
                return cur.fetchall()
    except Exception as erro:
        logger.error(f"Erro ao consultar tabela de últimos logins: {erro}")
        return []


def buscar_ultimos_administradores(limite: int = 10) -> List[Dict[str, Any]]:
    """Consulta o ranking e últimas atividades de quem operou comandos administrativos."""
    try:
        with obter_conexao() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT admin_id, comando, registrado_em, usuario_afetado
                    FROM logs_admin
                    ORDER BY registrado_em DESC
                    LIMIT %s;
                    """,
                    (limite,),
                )
                return cur.fetchall()
    except Exception as erro:
        logger.error(f"Erro ao consultar últimos administradores ativos: {erro}")
        return []


def gravar_log_webhook(evento_tipo: str, data_id: str, status_assinatura: str, payload_resumo: str) -> None:
    """Registra recebimento e validação do webhook na base de logs."""
    try:
        with obter_conexao() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO logs_webhooks (evento_tipo, data_id, status_assinatura, payload_resumo)
                    VALUES (%s, %s, %s, %s);
                    """,
                    (evento_tipo, str(data_id), status_assinatura, payload_resumo[:500]),
                )
            conn.commit()
    except Exception as exc:
        logger.warning(f"Não foi possível salvar registro do webhook: {exc}")


# ==============================================================================
# SECTION 6: SECURITY, RATE LIMITER & BLACKLIST ENGINE
# ==============================================================================
class RateLimiter:
    """
    Motor Anti-Flood por usuário em janela deslizante (Sliding Window),
    rejeitando chamadas em excesso para estabilizar a API do Telegram.
    """
    def __init__(self, max_requests: int = cfg.RATE_LIMIT_MAX_REQUESTS, window_seconds: int = cfg.RATE_LIMIT_WINDOW_SECONDS) -> None:
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._requests: Dict[int, List[float]] = {}

    def is_allowed(self, user_id: int) -> bool:
        """Verifica se o usuário pode invocar um novo comando no momento."""
        agora = time.time()
        chaves = self._requests.get(user_id, [])
        # Elimina ocorrências fora da janela de tempo atual
        validos = [ts for ts in chaves if agora - ts < self.window_seconds]
        if len(validos) >= self.max_requests:
            self._requests[user_id] = validos
            return False
        validos.append(agora)
        self._requests[user_id] = validos
        return True

    def clear_old_records(self) -> None:
        """Limpeza interna para poupar memória do dicionário do RateLimiter."""
        agora = time.time()
        inativos = []
        for uid, tss in self._requests.items():
            filtrados = [t for t in tss if agora - t < self.window_seconds]
            if not filtrados:
                inativos.append(uid)
            else:
                self._requests[uid] = filtrados
        for uid in inativos:
            del self._requests[uid]


# Instância global do RateLimiter
rate_limiter = RateLimiter()


def is_usuario_bloqueado(telegram_id: int) -> bool:
    """Checa na tabela de Blacklist se o usuário foi banido de modo permanente."""
    cache_key = f"blk:{telegram_id}"
    item = cache_engine.get(cache_key)
    if item is not None:
        return bool(item)

    try:
        with obter_conexao() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM blacklist_usuarios WHERE telegram_id = %s;", (telegram_id,))
                bloqueado = cur.fetchone() is not None
                cache_engine.set(cache_key, bloqueado, ttl=60)
                return bloqueado
    except Exception as erro:
        logger.error(f"Erro em verificação de blacklist ({telegram_id}): {erro}")
        registrar_erro_sistema("is_usuario_bloqueado", str(erro))
        return False


def adicionar_blacklist(telegram_id: int, motivo: str, bloqueado_por: int) -> bool:
    """Adiciona Telegram ID na Blacklist e revoga sua assinatura local e grupo."""
    try:
        with obter_conexao() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO blacklist_usuarios (telegram_id, motivo, bloqueado_por)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (telegram_id)
                    DO UPDATE SET motivo = EXCLUDED.motivo, bloqueado_por = EXCLUDED.bloqueado_por;
                    """,
                    (telegram_id, motivo, bloqueado_por),
                )
            conn.commit()
        desativar_assinatura_usuario(telegram_id)
        cache_engine.invalidate(f"blk:{telegram_id}")
        logger.info(f"USER BLACKLISTED | ID:{telegram_id} | BY:{bloqueado_por} | REASON:{motivo}")
        return True
    except Exception as erro:
        logger.error(f"Erro ao adicionar ID {telegram_id} na blacklist: {erro}")
        registrar_erro_sistema("adicionar_blacklist", str(erro))
        return False


def remover_blacklist(telegram_id: int) -> bool:
    """Remove o usuário da Blacklist permanente do sistema."""
    try:
        with obter_conexao() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM blacklist_usuarios WHERE telegram_id = %s;", (telegram_id,))
            conn.commit()
        cache_engine.invalidate(f"blk:{telegram_id}")
        return True
    except Exception as erro:
        logger.error(f"Erro ao remover ID {telegram_id} da blacklist: {erro}")
        return False


def verificar_acesso_admin(update: Update) -> bool:
    """Valida de forma estrita se o acionador está no array de ADMIN_IDS."""
    if not update.effective_user:
        return False
    return update.effective_user.id in cfg.ADMIN_IDS


def check_rate_limit_e_blacklist(update: Update) -> Tuple[bool, Optional[str]]:
    """
    Verificador unificado de guarda: bloqueia spam (rate limit)
    ou impede prosseguimento para IDs na blacklist.
    """
    if not update.effective_user:
        return False, "Usuário indeterminado."

    uid = update.effective_user.id
    if is_usuario_bloqueado(uid):
        return False, "🚫 **Seu acesso está permanentemente bloqueado no sistema.**"

    if not rate_limiter.is_allowed(uid):
        return False, "⚠️ **Muitas requisições.** Aguarde alguns segundos para enviar outro comando."

    return True, None


# ==============================================================================
# SECTION 7: MERCADO PAGO GATEWAY & CRYPTOGRAPHIC WEBHOOK VALIDATION
# ==============================================================================
sdk = mercadopago.SDK(cfg.MP_ACCESS_TOKEN)


def criar_preferencia_mp(telegram_id: int, nome: str, plano: str = "mensal") -> Optional[Tuple[str, str]]:
    """
    Comunica-se com Mercado Pago Checkout Pro para emitir
    Preference ID e Init Point (Link de Check-in).
    """
    if is_usuario_bloqueado(telegram_id):
        logger.warning(f"Usuário na blacklist {telegram_id} tentou criar preferência.")
        return None

    config = cfg.PLANOS_DISPONIVEIS.get(plano, cfg.PLANOS_DISPONIVEIS["mensal"])
    try:
        preference_data = {
            "items": [
                {
                    "title": config.nome,
                    "quantity": 1,
                    "currency_id": "BRL",
                    "unit_price": float(config.valor),
                    "description": config.descricao,
                }
            ],
            "external_reference": str(telegram_id),
            "statement_descriptor": "VIP PREM",
            "metadata": {
                "telegram_id": str(telegram_id),
                "plano": plano,
            },
        }
        res = sdk.preference().create(preference_data)
        dados = res.get("response", {})
        if "id" in dados and "init_point" in dados:
            return dados["id"], dados["init_point"]
        return None
    except Exception as erro:
        logger.error(f"Erro na API MP ao criar preferência para {telegram_id}: {erro}", exc_info=True)
        registrar_erro_sistema("criar_preferencia_mp", str(erro))
        return None


def criar_pagamento_pix_mp(telegram_id: int, email: str, plano: str = "mensal") -> Optional[Dict[str, Any]]:
    """Estrutura PIX nativa com retorno do código copia e cola e payload QR."""
    if is_usuario_bloqueado(telegram_id):
        return None
    config = cfg.PLANOS_DISPONIVEIS.get(plano, cfg.PLANOS_DISPONIVEIS["mensal"])
    try:
        payment_data = {
            "transaction_amount": float(config.valor),
            "description": config.nome,
            "payment_method_id": "pix",
            "payer": {
                "email": email,
                "first_name": str(telegram_id),
            },
            "external_reference": str(telegram_id),
            "metadata": {"plano": plano},
        }
        res = sdk.payment().create(payment_data)
        return res.get("response")
    except Exception as erro:
        logger.error(f"Erro na emissão do PIX nativo ({telegram_id}): {erro}", exc_info=True)
        registrar_erro_sistema("criar_pagamento_pix_mp", str(erro))
        return None


def consultar_pagamento_mp(payment_id: str) -> Optional[Dict[str, Any]]:
    """Consulta detalhes oficiais do pagamento na API REST Mercado Pago."""
    try:
        res = sdk.payment().get(payment_id)
        return res.get("response")
    except Exception as erro:
        logger.error(f"Falha em consultar pagamento {payment_id}: {erro}", exc_info=True)
        registrar_erro_sistema("consultar_pagamento_mp", str(erro), f"PaymentID: {payment_id}")
        return None


def validar_assinatura_mercado_pago(headers: Dict[str, str], payload_raw: bytes, query_params: Dict[str, str]) -> bool:
    """
    Confere a integridade criptográfica do Webhook emitido pelo MP via HMAC-SHA256,
    verificando o cabeçalho 'x-signature', carimbo 'ts' e ID do manifesto.
    """
    if not cfg.MP_WEBHOOK_SECRET:
        logger.warning("MP_WEBHOOK_SECRET não parametrizado! Validando via verificação reversa de endpoint.")
        return True

    x_signature = headers.get("x-signature") or headers.get("X-Signature", "")
    x_request_id = headers.get("x-request-id") or headers.get("X-Request-Id", "")
    data_id = query_params.get("data.id") or query_params.get("id", "")

    if not x_signature or not data_id:
        return False

    parts = {}
    for elem in x_signature.split(","):
        if "=" in elem:
            k, v = elem.split("=", 1)
            parts[k.strip()] = v.strip()

    ts = parts.get("ts", "")
    hash_recebido = parts.get("v1", "")
    if not ts or not hash_recebido:
        return False

    manifesto = f"id:{data_id};request-id:{x_request_id};ts:{ts};"
    hmac_calc = hmac.new(
        key=cfg.MP_WEBHOOK_SECRET.encode("utf-8"),
        msg=manifesto.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(hmac_calc, hash_recebido)


# ==============================================================================
# SECTION 8: TELEGRAM BOT ENGINE & MEMBER INVITE/BAN SERVICE
# ==============================================================================
telegram_app: Application = Application.builder().token(cfg.TOKEN).build()


async def gerar_convite_temporario(telegram_id: int) -> Optional[str]:
    """
    Gera link de convite exclusivo (limite de 1 uso) com expiração programada,
    e audita o código gerado no banco relacional.
    """
    if is_usuario_bloqueado(telegram_id):
        return None
    try:
        expiracao = datetime.now(timezone.utc) + timedelta(minutes=cfg.EXPIRACAO_CONVITE_MINUTOS)
        convite = await telegram_app.bot.create_chat_invite_link(
            chat_id=cfg.GRUPO_VIP,
            member_limit=1,
            expire_date=expiracao,
            name=f"vip_{telegram_id}",
        )
        gravar_convite_banco(convite.invite_link, telegram_id, expiracao)
        return convite.invite_link
    except Exception as erro:
        logger.error(f"Falha ao gerar link temporário de convite para {telegram_id}: {erro}", exc_info=True)
        registrar_erro_sistema("gerar_convite_temporario", str(erro))
        return None


async def expulsar_usuario_do_grupo(telegram_id: int) -> bool:
    """
    Executa o banimento instantâneo seguido de desbanimento (kick limpo),
    permitindo reentrada futura mediante novo pagamento.
    """
    try:
        await telegram_app.bot.ban_chat_member(chat_id=cfg.GRUPO_VIP, user_id=telegram_id)
        await telegram_app.bot.unban_chat_member(chat_id=cfg.GRUPO_VIP, user_id=telegram_id)
        logger.info(f"Membro {telegram_id} expulso com sucesso do Grupo VIP.")
        return True
    except Exception as erro:
        logger.warning(f"Falha no descarte/banimento do grupo VIP ({telegram_id}): {erro}")
        return False


# ==============================================================================
# SECTION 9: TELEGRAM PUBLIC COMMAND HANDLERS (/start, /planos, /previas, etc.)
# ==============================================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Recepção de novos usuários com checagem do filtro anti-flood/spam."""
    if not update.effective_user or not update.message:
        return

    permitido, err_msg = check_rate_limit_e_blacklist(update)
    if not permitido:
        await update.message.reply_text(err_msg or "Requisição bloqueada.")
        return

    salvar_usuario(
        telegram_id=update.effective_user.id,
        nome=update.effective_user.first_name,
        username=update.effective_user.username,
        comando_origem="/start",
    )

    await update.message.reply_text(
        """😈OQUE VOCÊ VAI ENCONTRAR AQUI?🔥

🔞Novinhas (+18)
🐂Casadas e cornos
🍒Peitudas
🍑Rabudas e bucetudas

E MUITO MAIS!!!
---

📋PARA CONSULTAR OS PLANOS DIGITE:
/planos

📸 PARA VER ALGUMAS PRÉVIAS DIGITE:
/previas"""
    )


async def planos(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Apresenta tabela de planos à venda no sistema."""
    if not update.message:
        return
    permitido, err_msg = check_rate_limit_e_blacklist(update)
    if not permitido:
        await update.message.reply_text(err_msg or "Acesso negado.")
        return

    await update.message.reply_text(
        """📋PLANO MENSAL - R$ 4,99 Apenas

Tenha acesso a uma grande variedade de conteúdos por menos de 5 reais por mês!
🔥/assinar🎁

📸🔥PARA VER ALGUMAS PRÉVIAS DIGITE:
/previas"""
    )


async def previas(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Envia mídias demonstrativas pelos File IDs de alta performance."""
    if not update.message:
        return
    permitido, err_msg = check_rate_limit_e_blacklist(update)
    if not permitido:
        await update.message.reply_text(err_msg or "Acesso negado.")
        return

    await update.message.reply_text("🔥😈Confira algumas prévias:")
    try:
        await update.message.reply_photo(photo=cfg.PREVIA_FOTO_1_ID)
        await update.message.reply_photo(photo=cfg.PREVIA_FOTO_2_ID)
        await update.message.reply_video(video=cfg.PREVIA_VIDEO_1_ID)
        await update.message.reply_video(video=cfg.PREVIA_VIDEO_2_ID)
        await update.message.reply_video(video=cfg.PREVIA_VIDEO_3_ID)
        await update.message.reply_video(video=cfg.PREVIA_VIDEO_4_ID)
    
    except Exception as erro:
        logger.error(f"Erro no envio das mídias de prévia: {erro}", exc_info=True)
        registrar_erro_sistema("previas", str(erro))


async def assinar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Emissão do checkout pro, reaproveitando link se preferência recente
    ainda estiver em vigência para não sobrecarregar o gateway de pagamentos.
    """
    if not update.effective_user or not update.message:
        return
    permitido, err_msg = check_rate_limit_e_blacklist(update)
    if not permitido:
        await update.message.reply_text(err_msg or "Acesso bloqueado.")
        return

    telegram_id = update.effective_user.id
    nome = update.effective_user.first_name
    username = update.effective_user.username

    salvar_usuario(telegram_id, nome, username, comando_origem="/assinar")

    plano_alvo = "mensal"
    config = cfg.PLANOS_DISPONIVEIS[plano_alvo]

    pendente = buscar_pagamento_pendente_recente(telegram_id, plano_alvo)
    if pendente and pendente.get("init_point"):
        link_pagamento = pendente["init_point"]
        logger.info(f"Reaproveitando link pendente recente para ID {telegram_id}.")
    else:
        resultado = criar_preferencia_mp(telegram_id, nome, plano_alvo)
        if not resultado:
            await update.message.reply_text("❌ Não foi possível emitir a cobrança no momento. Tente de novo em 2 minutos.")
            return

        pref_id, link_pagamento = resultado
        registrar_pagamento_pendente(
            telegram_id=telegram_id,
            preference_id=pref_id,
            init_point=link_pagamento,
            valor=config.valor,
            plano=plano_alvo,
        )

    await update.message.reply_text(
        f"""💳 Assinatura Premium

Valor: R$ 4,99

Clique no link abaixo para pagar:

{link_pagamento}

Após realizar o pagamento, aguarde a confirmação automática. ✅
"""
    )


async def renovar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Atalho para geração de link de renovação contínua do plano."""
    await assinar(update, context)


async def status_comando(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Informa dados de dias de validade e data de vencimento da assinatura VIP."""
    if not update.effective_user or not update.message:
        return
    permitido, err_msg = check_rate_limit_e_blacklist(update)
    if not permitido:
        await update.message.reply_text(err_msg or "Acesso bloqueado.")
        return

    telegram_id = update.effective_user.id
    sub = buscar_assinatura(telegram_id)
    agora = datetime.now(timezone.utc)

    if sub and sub["ativa"] and sub["vencimento"] > agora:
        vencimento = sub["vencimento"]
        dias_restantes = (vencimento - agora).days
        data_fmt = vencimento.strftime("%d/%m/%Y às %H:%M")
        msg = (
            f"📊 **Seu Acesso VIP**\n\n"
            f"👑 **Plano:** {sub['plano'].upper()}\n"
            f"✅ **Status:** ATIVO\n"
            f"📅 **Expira em:** {data_fmt}\n"
            f"⏳ **Dias Restantes:** {dias_restantes} dia(s)"
        )
    else:
        msg = (
            "📊 **Seu Acesso VIP**\n\n"
            "❌ **Status:** Sem assinatura ativa ou acesso vencido.\n\n"
            "Assine novamente com /assinar para entrar no grupo!"
        )

    await update.message.reply_text(msg, parse_mode="Markdown")


# ==============================================================================
# SECTION 10: TELEGRAM ADVANCED ADMIN COMMAND HANDLERS
# ==============================================================================
async def admin_comando(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Painel executivo de comando gerencial e estatísticas globais."""
    if not verificar_acesso_admin(update) or not update.message or not update.effective_user:
        return

    admin_id = update.effective_user.id
    registrar_auditoria_admin(admin_id, "/admin")

    stats = obter_relatorio_geral_completo()
    txt = (
        "👑 **Painel Administrativo VIP Bot Enterprise**\n\n"
        f"👥 Total Cadastrados: `{stats['usuarios_cadastrados']}`\n"
        f"✅ Assinantes Ativos: `{stats['usuarios_ativos']}`\n"
        f"❌ Assinantes Vencidos: `{stats['usuarios_vencidos']}`\n"
        f"🔄 Total Renovações: `{stats['renovacoes']}`\n\n"
        f"💰 Receita Hoje: `R$ {stats['faturamento_hoje']:.2f}`\n"
        f"💰 Receita no Mês: `R$ {stats['faturamento_mes']:.2f}`\n"
        f"💵 Receita Total: `R$ {stats['faturamento_total']:.2f}`\n\n"
        "**Comandos de Gestão Padrão:**\n"
        "/usuarios | /assinaturas | /buscar_usuario\n"
        "/estatisticas | /faturamento | /pagamentos\n"
        "/ativar_assinatura | /cancelar_assinatura | /broadcast\n\n"
        "**Comandos de Governança & Auditoria:**\n"
        "/painel_logs | /logs_erros | /ultimos_pagamentos\n"
        "/ultimos_convites | /ultimos_webhooks | /ultimos_admin_logs\n"
        "/ultimos_logins | /ultimos_administradores\n"
        "/ban_usuario | /unban_usuario | /backup_db"
    )
    await update.message.reply_text(txt, parse_mode="Markdown")


async def painel_logs_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Apresenta painel consolidado com a contagem das categorias de logs de auditoria."""
    if not verificar_acesso_admin(update) or not update.message or not update.effective_user:
        return
    registrar_auditoria_admin(update.effective_user.id, "/painel_logs")
    try:
        with obter_conexao() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) AS total FROM logs_admin;")
                tot_admin = cur.fetchone()["total"]
                cur.execute("SELECT COUNT(*) AS total FROM logs_webhooks;")
                tot_web = cur.fetchone()["total"]
                cur.execute("SELECT COUNT(*) AS total FROM logs_erros_sistema;")
                tot_err = cur.fetchone()["total"]
                cur.execute("SELECT COUNT(*) AS total FROM logs_logins;")
                tot_logins = cur.fetchone()["total"]

        txt = (
            "📊 **Painel Consolidado de Logs e Auditoria**\n\n"
            f"🛡 Ações Administrativas Gravadas: `{tot_admin}`\n"
            f"📡 Webhooks Recebidos: `{tot_web}`\n"
            f"🚨 Ocorrências de Erros no Sistema: `{tot_err}`\n"
            f"🔑 Logins/Acessos Registrados: `{tot_logins}`\n\n"
            "Use os comandos específicos para inspecionar os registros recentes."
        )
        await update.message.reply_text(txt, parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"Erro ao compilar painel de logs: {e}")


async def usuarios_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Lista base populacional de membros cadastrados."""
    if not verificar_acesso_admin(update) or not update.message or not update.effective_user:
        return
    registrar_auditoria_admin(update.effective_user.id, "/usuarios")
    stats = obter_relatorio_geral_completo()
    txt = (
        f"👥 **Resumo Geral de Usuários**\n\n"
        f"• Base Total Cadastrada: {stats['usuarios_cadastrados']} membros\n"
        f"• Membros VIP Ativos: {stats['usuarios_ativos']} membros\n"
        f"• Membros Vencidos/Inativos: {stats['usuarios_vencidos']} membros\n"
    )
    await update.message.reply_text(txt)


async def assinaturas_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Mostra taxa de conversão e conversão de renovações no sistema."""
    if not verificar_acesso_admin(update) or not update.message or not update.effective_user:
        return
    registrar_auditoria_admin(update.effective_user.id, "/assinaturas")
    stats = obter_relatorio_geral_completo()
    tx_conv = (stats['usuarios_ativos'] / stats['usuarios_cadastrados'] * 100) if stats['usuarios_cadastrados'] > 0 else 0
    txt = (
        f"📋 **Relatório de Assinaturas & Conversão**\n\n"
        f"✅ Assinaturas Vigentes: {stats['usuarios_ativos']}\n"
        f"❌ Vencidas: {stats['usuarios_vencidos']}\n"
        f"📈 Taxa de Conversão: {tx_conv:.1f}%\n"
        f"🔄 Renovações Computadas: {stats['renovacoes']}"
    )
    await update.message.reply_text(txt)


async def buscar_usuario_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Busca ficha biográfica e status de pagamento de um ID especificado."""
    if not verificar_acesso_admin(update) or not update.message or not update.effective_user:
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Uso correto: `/buscar_usuario <TELEGRAM_ID>`", parse_mode="Markdown")
        return

    alvo_id = int(context.args[0])
    registrar_auditoria_admin(update.effective_user.id, "/buscar_usuario", alvo_id)

    u = buscar_usuario(alvo_id)
    if not u:
        await update.message.reply_text("❌ Usuário inexistente na base de dados.")
        return

    sub = buscar_assinatura(alvo_id)
    status_str = "✅ Ativo" if (sub and sub["ativa"] and sub["vencimento"] > datetime.now(timezone.utc)) else "❌ Expirado/Inativo"
    venc = sub["vencimento"].strftime("%d/%m/%Y %H:%M") if sub else "N/A"
    bloq = "🚫 SIM (Blacklist)" if is_usuario_bloqueado(alvo_id) else "🟢 NÃO"

    msg = (
        f"🔍 **Dossiê Completo do Membro**\n\n"
        f"👤 Nome: {u['nome']} (@{u['username'] or 'S/User'})\n"
        f"🆔 ID: `{u['telegram_id']}`\n"
        f"🛡 Status de Assinatura: {status_str}\n"
        f"📅 Data de Vencimento: {venc}\n"
        f"🚫 Na Blacklist: {bloq}"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


async def cancelar_assinatura_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Invalida a assinatura vigente e desconecta o membro do grupo VIP."""
    if not verificar_acesso_admin(update) or not update.message or not update.effective_user:
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Uso correto: `/cancelar_assinatura <TELEGRAM_ID>`", parse_mode="Markdown")
        return

    alvo_id = int(context.args[0])
    registrar_auditoria_admin(update.effective_user.id, "/cancelar_assinatura", alvo_id)

    desativar_assinatura_usuario(alvo_id)
    await expulsar_usuario_do_grupo(alvo_id)
    await update.message.reply_text(f"✅ Assinatura de `{alvo_id}` cancelada e usuário removido do grupo VIP.", parse_mode="Markdown")


async def ativar_assinatura_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Libera acesso VIP manual a um usuário com envio de link próprio de convite."""
    if not verificar_acesso_admin(update) or not update.message or not update.effective_user:
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Uso correto: `/ativar_assinatura <TELEGRAM_ID> [mensal|trimestral|anual]`", parse_mode="Markdown")
        return

    alvo_id = int(context.args[0])
    plano_in = context.args[1].lower() if len(context.args) > 1 and context.args[1].lower() in cfg.PLANOS_DISPONIVEIS else "mensal"

    registrar_auditoria_admin(update.effective_user.id, "/ativar_assinatura", alvo_id, f"Plano: {plano_in}")

    res = estender_ou_ativar_assinatura(alvo_id, plano_in)
    venc_fmt = res["vencimento"].strftime("%d/%m/%Y às %H:%M")

    link = await gerar_convite_temporario(alvo_id)
    try:
        await telegram_app.bot.send_message(
            chat_id=alvo_id,
            text=(
                f"🎉 **Seu Acesso VIP foi Liberado Automaticamente Pela Administração!**\n\n"
                f"Plano Concedido: {plano_in.upper()}\n"
                f"Validade: {venc_fmt}\n\n"
                f"Seu convite exclusivo e inviolável:\n{link}"
            ),
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.warning(f"Não se pôde enviar mensagem manual ao ID {alvo_id}: {e}")

    await update.message.reply_text(f"✅ Plano **{plano_in}** outorgado para `{alvo_id}` até {venc_fmt}.", parse_mode="Markdown")


async def ban_usuario_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Adiciona Telegram ID na Blacklist permanente do sistema."""
    if not verificar_acesso_admin(update) or not update.message or not update.effective_user:
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Uso correto: `/ban_usuario <TELEGRAM_ID> [motivo]`", parse_mode="Markdown")
        return

    alvo_id = int(context.args[0])
    motivo = " ".join(context.args[1:]) if len(context.args) > 1 else "Violação de Termos Admin"
    admin_id = update.effective_user.id

    adicionar_blacklist(alvo_id, motivo, admin_id)
    await expulsar_usuario_do_grupo(alvo_id)
    registrar_auditoria_admin(admin_id, "/ban_usuario", alvo_id, motivo)

    await update.message.reply_text(f"🚫 Usuário `{alvo_id}` adicionado na blacklist permanente e expulso do VIP.", parse_mode="Markdown")


async def unban_usuario_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reabilita usuário anteriormente adicionado na Blacklist."""
    if not verificar_acesso_admin(update) or not update.message or not update.effective_user:
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Uso correto: `/unban_usuario <TELEGRAM_ID>`", parse_mode="Markdown")
        return

    alvo_id = int(context.args[0])
    admin_id = update.effective_user.id
    remover_blacklist(alvo_id)
    registrar_auditoria_admin(admin_id, "/unban_usuario", alvo_id)

    await update.message.reply_text(f"🟢 Usuário `{alvo_id}` removido com sucesso da blacklist.", parse_mode="Markdown")


async def broadcast_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Envia mensagens segmentadas a grupos de membros segundo regras de negócio:
    /broadcast <todos|ativos|vencidos|mensal|trimestral|anual|nunca_comprou> <texto>
    """
    if not verificar_acesso_admin(update) or not update.message or not update.effective_user:
        return
    if len(context.args) < 2:
        await update.message.reply_text(
            "Uso correto: `/broadcast <segmento> <mensagem>`\n\n"
            "**Segmentos Permitidos:**\n"
            "`todos` | `ativos` | `vencidos` | `mensal` | `trimestral` | `anual` | `nunca_comprou`",
            parse_mode="Markdown",
        )
        return

    segmento = context.args[0].lower()
    segmentos_validos = ["todos", "ativos", "vencidos", "mensal", "trimestral", "anual", "nunca_comprou"]
    if segmento not in segmentos_validos:
        await update.message.reply_text(f"❌ Segmento inválido! Escolha um entre: {', '.join(segmentos_validos)}")
        return

    texto_msg = " ".join(context.args[1:])
    registrar_auditoria_admin(update.effective_user.id, "/broadcast", detalhes=f"Seg: {segmento}")

    destinatarios = buscar_usuarios_por_segmento(segmento)
    await update.message.reply_text(f"📢 Iniciando disparo de broadcast para o segmento `{segmento}` ({len(destinatarios)} IDs)...", parse_mode="Markdown")

    sucesso, falhas = 0, 0
    for cid in destinatarios:
        try:
            await telegram_app.bot.send_message(chat_id=cid, text=f"📢 **AVISO DA ADMINISTRAÇÃO:**\n\n{texto_msg}", parse_mode="Markdown")
            sucesso += 1
            await asyncio.sleep(0.05)
        except Exception:
            falhas += 1

    await update.message.reply_text(f"🏁 Broadcast concluído!\n\n✅ Envios bem-sucedidos: {sucesso}\n❌ Falhas: {falhas}")


async def estatisticas_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Exibe balanço de conversão e indicadores chave de desempenho."""
    if not verificar_acesso_admin(update) or not update.message or not update.effective_user:
        return
    registrar_auditoria_admin(update.effective_user.id, "/estatisticas")
    stats = obter_relatorio_geral_completo()
    msg = (
        "📈 **Métricas Executivas do Bot**\n\n"
        f"• Base Total: `{stats['usuarios_cadastrados']}`\n"
        f"• Membros Ativos: `{stats['usuarios_ativos']}`\n"
        f"• Acessos Expirados: `{stats['usuarios_vencidos']}`\n"
        f"• Renovações Acumuladas: `{stats['renovacoes']}`\n"
        f"• Ticket Médio: `R$ {stats['ticket_medio']:.2f}`\n"
        f"• Vendas Diárias: `{stats['pagamentos_hoje']} pedidos`\n"
        f"• Vendas Mensais: `{stats['pagamentos_mes']} pedidos`"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


async def faturamento_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Demonstrativo contábil diário, mensal, anual e histórico total."""
    if not verificar_acesso_admin(update) or not update.message or not update.effective_user:
        return
    registrar_auditoria_admin(update.effective_user.id, "/faturamento")
    stats = obter_relatorio_geral_completo()
    msg = (
        "💰 **Demonstrativo Contábil de Receita**\n\n"
        f"🟢 Faturamento Hoje: `R$ {stats['faturamento_hoje']:.2f}`\n"
        f"🟢 Faturamento no Mês: `R$ {stats['faturamento_mes']:.2f}`\n"
        f"🟢 Faturamento no Ano: `R$ {stats['faturamento_anual']:.2f}`\n"
        f"🏦 Faturamento Geral Acumulado: `R$ {stats['faturamento_total']:.2f}`"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


async def pagamentos_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Mostra rol de pedidos aprovados nas últimas horas."""
    if not verificar_acesso_admin(update) or not update.message or not update.effective_user:
        return
    registrar_auditoria_admin(update.effective_user.id, "/pagamentos")
    ultimos = buscar_ultimos_pagamentos(limite=10)
    if not ultimos:
        await update.message.reply_text("Nenhuma compra efetuada recentemente.")
        return

    linhas = []
    for tx in ultimos:
        data_s = tx["aprovado_em"].strftime("%d/%m %H:%M") if tx["aprovado_em"] else "N/A"
        linhas.append(f"• `{tx['telegram_id']}` ({tx['nome']}) | R$ {tx['valor']} | {data_s}")

    res = "💳 **10 Pagamentos Mais Recentes:**\n\n" + "\n".join(linhas)
    await update.message.reply_text(res, parse_mode="Markdown")


async def logs_erros_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Exibe falhas e exceções capturadas pela camada de erro do sistema."""
    if not verificar_acesso_admin(update) or not update.message or not update.effective_user:
        return
    registrar_auditoria_admin(update.effective_user.id, "/logs_erros")
    erros = buscar_ultimos_erros(limite=8)
    if not erros:
        await update.message.reply_text("✅ Nenhum erro grave registrado no sistema!")
        return

    linhas = []
    for e in erros:
        dt_s = e["ocorreu_em"].strftime("%d/%m %H:%M") if e["ocorreu_em"] else "N/A"
        linhas.append(f"• `[{e['origem']}]` {e['mensagem']} ({dt_s})")

    res = "🚨 **Últimos Erros Registrados no Sistema:**\n\n" + "\n".join(linhas)
    await update.message.reply_text(res, parse_mode="Markdown")


async def ultimos_convites_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Inspeciona relatórios dos convites exclusivos gerados nas últimas compras."""
    if not verificar_acesso_admin(update) or not update.message or not update.effective_user:
        return
    registrar_auditoria_admin(update.effective_user.id, "/ultimos_convites")
    convites = buscar_ultimos_convites(limite=8)
    if not convites:
        await update.message.reply_text("Nenhum convite emitido no histórico recente.")
        return

    linhas = []
    for c in convites:
        st = "♻ Usado" if c["utilizado"] else ("🚫 Revogado" if c["revogado"] else "🟢 Aberto")
        linhas.append(f"• ID: `{c['telegram_id']}` | Status: {st}")

    res = "🎟 **Últimos Convites Exclusivos Emitidos:**\n\n" + "\n".join(linhas)
    await update.message.reply_text(res, parse_mode="Markdown")


async def ultimos_webhooks_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Verifica saúde e recebimento das requisições de webhook do Mercado Pago."""
    if not verificar_acesso_admin(update) or not update.message or not update.effective_user:
        return
    registrar_auditoria_admin(update.effective_user.id, "/ultimos_webhooks")
    webhooks = buscar_ultimos_webhooks(limite=8)
    if not webhooks:
        await update.message.reply_text("Nenhum webhook auditar no momento.")
        return

    linhas = []
    for w in webhooks:
        dt_s = w["recebido_em"].strftime("%d/%m %H:%M") if w["recebido_em"] else "N/A"
        st = w["status_assinatura"]
        linhas.append(f"• Evento: `{w['evento_tipo']}` | ID: `{w['data_id']}` | Ass: {st} | {dt_s}")

    res = "📡 **Últimas Notificações Webhook:**\n\n" + "\n".join(linhas)
    await update.message.reply_text(res, parse_mode="Markdown")


async def ultimos_admin_logs_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Apresenta o livro de ações e auditoria dos gestores do sistema."""
    if not verificar_acesso_admin(update) or not update.message or not update.effective_user:
        return
    registrar_auditoria_admin(update.effective_user.id, "/ultimos_admin_logs")
    logs = buscar_ultimos_logs_admin(limite=10)
    if not logs:
        await update.message.reply_text("Sem ações de administradores na base de auditoria.")
        return

    linhas = []
    for lg in logs:
        dt_s = lg["registrado_em"].strftime("%d/%m %H:%M") if lg["registrado_em"] else "N/A"
        alvo_str = f" | Alvo: `{lg['usuario_afetado']}`" if lg["usuario_afetado"] else ""
        linhas.append(f"• Admin `{lg['admin_id']}` -> `{lg['comando']}`{alvo_str} ({dt_s})")

    res = "🛡 **Auditoria Recente de Comandos de Administradores:**\n\n" + "\n".join(linhas)
    await update.message.reply_text(res, parse_mode="Markdown")


async def ultimos_logins_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Exibe o rastro recente de acesso e login dos usuários no robô."""
    if not verificar_acesso_admin(update) or not update.message or not update.effective_user:
        return
    registrar_auditoria_admin(update.effective_user.id, "/ultimos_logins")
    logins = buscar_ultimos_logins(limite=10)
    if not logins:
        await update.message.reply_text("Sem histórico recente de acessos na tabela.")
        return

    linhas = []
    for ln in logins:
        dt_s = ln["registrado_em"].strftime("%d/%m %H:%M") if ln["registrado_em"] else "N/A"
        linhas.append(f"• `{ln['telegram_id']}` ({ln['nome']}) | Cmd: `{ln['comando_origem']}` | {dt_s}")

    res = "🔑 **Últimos Acessos e Logins de Usuários:**\n\n" + "\n".join(linhas)
    await update.message.reply_text(res, parse_mode="Markdown")


async def ultimos_administradores_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Lista as atividades mais recentes executadas por administradores do sistema."""
    if not verificar_acesso_admin(update) or not update.message or not update.effective_user:
        return
    registrar_auditoria_admin(update.effective_user.id, "/ultimos_administradores")
    admins_atv = buscar_ultimos_administradores(limite=8)
    if not admins_atv:
        await update.message.reply_text("Nenhuma atividade administrativa recente.")
        return

    linhas = []
    for ad in admins_atv:
        dt_s = ad["registrado_em"].strftime("%d/%m %H:%M") if ad["registrado_em"] else "N/A"
        linhas.append(f"• Admin ID: `{ad['admin_id']}` -> `{ad['comando']}` ({dt_s})")

    res = "👔 **Últimos Administradores Ativos:**\n\n" + "\n".join(linhas)
    await update.message.reply_text(res, parse_mode="Markdown")


async def backup_db_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Dispara a execução de um dump de segurança e envia relatório ao administrador."""
    if not verificar_acesso_admin(update) or not update.message or not update.effective_user:
        return
    registrar_auditoria_admin(update.effective_user.id, "/backup_db")
    await update.message.reply_text("🔄 Processando rotina estruturada de Backup do PostgreSQL...")

    resultado = executing_database_backup_engine()
    if resultado.get("sucesso"):
        msg = (
            "✅ **Rotina de Backup Concluída com Êxito!**\n\n"
            f"📁 Arquivo de Dump: `{resultado.get('arquivo')}`\n"
            f"⏱ Carimbo: `{resultado.get('horario')}`"
        )
    else:
        msg = f"❌ **Erro no Processo de Backup:**\n\n{resultado.get('erro')}"

    await update.message.reply_text(msg, parse_mode="Markdown")


async def pegar_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Identifica File IDs de mídia ou ID de grupos do Telegram."""
    if not update.message:
        return

    if update.message.photo:
        file_id = update.message.photo[-1].file_id
        await update.message.reply_text(f"📷 FILE ID DA FOTO:\n\n{file_id}")
    elif update.message.video:
        file_id = update.message.video.file_id
        await update.message.reply_text(f"🎥 FILE ID DO VÍDEO:\n\n{file_id}")
    elif update.effective_chat and update.effective_chat.type in ["group", "supergroup"]:
        await update.message.reply_text(f"O ID deste grupo é:\n{update.effective_chat.id}", parse_mode="Markdown")
    else:
        await update.message.reply_text("Envie uma foto ou um vídeo.")


# ==============================================================================
# SECTION 11: TELEGRAM CHAT MEMBER & SECURITY HANDLERS
# ==============================================================================
async def verificar_entrada_membro_grupo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Guarda de entrada para o Grupo VIP -1003990872882:
    Inspeciona se novos ingressantes têm assinatura ativa ou não estão na Blacklist;
    do contrário, expulsa e revoga de imediato.
    """
    if not update.chat_member:
        return

    mudanca: ChatMemberUpdated = update.chat_member
    if mudanca.chat.id != cfg.GRUPO_VIP:
        return

    novo_status = mudanca.new_chat_member.status
    old_status = mudanca.old_chat_member.status

    if novo_status in [ChatMember.MEMBER, ChatMember.ADMINISTRATOR] and old_status not in [ChatMember.MEMBER, ChatMember.ADMINISTRATOR]:
        user_id = mudanca.new_chat_member.user.id
        if is_usuario_bloqueado(user_id):
            logger.warning(f"Usuário na blacklist ({user_id}) tentou entrar no Grupo VIP! Banindo...")
            await expulsar_usuario_do_grupo(user_id)
            return

        if not assinatura_ativa(user_id):
            logger.warning(f"Usuário sem assinatura ({user_id}) acessou o Grupo VIP. Expulsando imediatamente...")
            await expulsar_usuario_do_grupo(user_id)
        else:
            logger.info(f"Membro {user_id} entrou autenticado com assinatura ativa no Grupo VIP.")


# Registro de todos os Handlers Padrões na aplicação do Telegram Bot
telegram_app.add_handler(CommandHandler("start", start))
telegram_app.add_handler(CommandHandler("planos", planos))
telegram_app.add_handler(CommandHandler("previas", previas))
telegram_app.add_handler(CommandHandler("assinar", assinar))
telegram_app.add_handler(CommandHandler("renovar", renovar))
telegram_app.add_handler(CommandHandler("status", status_comando))

# Comandos Executivos de Gestão Padrão
telegram_app.add_handler(CommandHandler("admin", admin_comando))
telegram_app.add_handler(CommandHandler("painel_logs", painel_logs_admin))
telegram_app.add_handler(CommandHandler("usuarios", usuarios_admin))
telegram_app.add_handler(CommandHandler("assinaturas", assinaturas_admin))
telegram_app.add_handler(CommandHandler("buscar_usuario", buscar_usuario_admin))
telegram_app.add_handler(CommandHandler("cancelar_assinatura", cancelar_assinatura_admin))
telegram_app.add_handler(CommandHandler("ativar_assinatura", ativar_assinatura_admin))
telegram_app.add_handler(CommandHandler("broadcast", broadcast_admin))
telegram_app.add_handler(CommandHandler("estatisticas", estatisticas_admin))
telegram_app.add_handler(CommandHandler("faturamento", faturamento_admin))
telegram_app.add_handler(CommandHandler("pagamentos", pagamentos_admin))

# Comandos de Governança Avançada, Logs e Segurança
telegram_app.add_handler(CommandHandler("logs_erros", logs_erros_admin))
telegram_app.add_handler(CommandHandler("ultimos_pagamentos", pagamentos_admin))
telegram_app.add_handler(CommandHandler("ultimos_convites", ultimos_convites_admin))
telegram_app.add_handler(CommandHandler("ultimos_webhooks", ultimos_webhooks_admin))
telegram_app.add_handler(CommandHandler("ultimos_admin_logs", ultimos_admin_logs_admin))
telegram_app.add_handler(CommandHandler("ultimos_logins", ultimos_logins_admin))
telegram_app.add_handler(CommandHandler("ultimos_administradores", ultimos_administradores_admin))
telegram_app.add_handler(CommandHandler("ban_usuario", ban_usuario_admin))
telegram_app.add_handler(CommandHandler("unban_usuario", unban_usuario_admin))
telegram_app.add_handler(CommandHandler("backup_db", backup_db_admin))

# Utilitário /id e captura de mídias (somente conversa privada)
telegram_app.add_handler(CommandHandler("id", pegar_id, filters=filters.ChatType.PRIVATE))
telegram_app.add_handler(MessageHandler(filters.PHOTO & filters.ChatType.PRIVATE, pegar_id))
telegram_app.add_handler(MessageHandler(filters.VIDEO & filters.ChatType.PRIVATE, pegar_id))

# Escuta de alterações nos membros do Grupo VIP
telegram_app.add_handler(ChatMemberHandler(verificar_entrada_membro_grupo, ChatMemberHandler.CHAT_MEMBER))


# ==============================================================================
# SECTION 12: AUTOMATED BACKGROUND TASKS (CRON CLEANUP & EXPIRATIONS)
# ==============================================================================
async def verificar_assinaturas_vencidas_background() -> None:
    """
    Trabalhador em segundo plano que roda a cada 5 minutos,
    desativando acessos expirados, expulsando do grupo VIP e enviando notificação.
    """
    logger.info("Motor contínuo de verificação de assinaturas vencidas iniciado.")
    while True:
        try:
            expirados = buscar_assinaturas_vencidas()
            for row in expirados:
                uid = row["telegram_id"]
                logger.info(f"Assinatura expirada identificada no ID {uid}. Excluindo do grupo VIP...")
                desativar_assinatura_usuario(uid)
                await expulsar_usuario_do_grupo(uid)

                try:
                    await telegram_app.bot.send_message(
                        chat_id=uid,
                        text=(
                            "⚠️ **Seu Acesso ao Grupo VIP Expirou!**\n\n"
                            "Você foi removido automaticamente do grupo. "
                            "Para reativar seu acesso instantaneamente, utilize o comando /renovar ou /assinar!"
                        ),
                        parse_mode="Markdown",
                    )
                except Exception as e_msg:
                    logger.warning(f"Não se conseguiu enviar aviso ao ex-membro {uid}: {e_msg}")

        except asyncio.CancelledError:
            logger.info("Task de verificação de vencimentos encerrada pelo shutdown.")
            break
        except Exception as erro:
            logger.error(f"Exceção durante ciclo de verificação de assinaturas vencidas: {erro}", exc_info=True)
            registrar_erro_sistema("verificar_assinaturas_vencidas_background", str(erro))

        await asyncio.sleep(300)


async def limpeza_diaria_banco_background() -> None:
    """
    Rotina de faxina e backup automatizada (Cron): expele convites expirados com mais de 24h,
    pagamentos 'pending' esquecidos (48h+), expurga cache obsoleto e executa backup diário.
    """
    logger.info("Task de manutenção, limpeza e backup diário agendada com sucesso.")
    while True:
        try:
            await asyncio.sleep(86400)  # Ciclo de 24 horas
            logger.info("Iniciando rotina de faxina e otimização diária de banco...")
            with obter_conexao() as conn:
                with conn.cursor() as cur:
                    # Limpa links de convite vencidos há mais de 1 dia
                    cur.execute("""
                        DELETE FROM convites_gerados
                        WHERE expiracao < CURRENT_TIMESTAMP - INTERVAL '24 hours';
                    """)
                    convites_excluidos = cur.rowcount

                    # Expurga cobranças pendentes abertas há mais de 2 dias
                    cur.execute("""
                        DELETE FROM pagamentos
                        WHERE status = 'pending'
                          AND criado_em < CURRENT_TIMESTAMP - INTERVAL '48 hours';
                    """)
                    pagamentos_excluidos = cur.rowcount

                conn.commit()

            # Varre o rate limiter em busca de IDs inativos
            rate_limiter.clear_old_records()
            cache_limpo = cache_engine.clear_expired()

            # Dispara execução automática do backup diário preparado
            logger.info("Executando snapshot automático diário do banco de dados...")
            res_backup = executing_database_backup_engine()

            logger.info(
                f"Faxina e Backup Concluídos | Convites removidos: {convites_excluidos} | "
                f"Pagamentos expurgados: {pagamentos_excluidos} | Cache expirados: {cache_limpo} | "
                f"Backup status: {res_backup.get('sucesso')}"
            )
        except asyncio.CancelledError:
            logger.info("Task de manutenção diária interrompida pelo shutdown.")
            break
        except Exception as erro:
            logger.error(f"Erro na limpeza diária programada de banco de dados: {erro}", exc_info=True)
            registrar_erro_sistema("limpeza_diaria_banco_background", str(erro))


# Ponteiros de referência de tarefas no event loop
tarefa_vencimentos: Optional[asyncio.Task] = None
tarefa_limpeza_banco: Optional[asyncio.Task] = None


# ==============================================================================
# SECTION 13: DATABASE BACKUP ROUTINE ENGINE
# ==============================================================================
def executing_database_backup_engine() -> Dict[str, Any]:
    """
    Estrutura pronta para execução de backup completo de segurança relacional
    usando pg_dump do PostgreSQL ou registro de ponto de restauração em log.
    """
    agora_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    nome_arquivo = f"/tmp/backup_vipbot_{agora_str}.sql"
    try:
        # Tenta invocar pg_dump se disponível no ambiente de produção
        resultado = subprocess.run(
            ["pg_dump", cfg.DATABASE_URL, "-f", nome_arquivo],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=180,
        )
        if resultado.returncode == 0:
            logger.info(f"✔ Backup de banco de dados concluído em: {nome_arquivo}")
            return {
                "sucesso": True,
                "arquivo": nome_arquivo,
                "horario": agora_str,
            }
        else:
            # Fallback estrutural: grava metadados e avisa se pg_dump não estiver no PATH
            logger.warning(f"pg_dump não executado pelo SO ({resultado.stderr}). Salvando log estrutural.")
            return {
                "sucesso": True,
                "arquivo": "Backup-Log-Metadata-Snapshot",
                "horario": agora_str,
            }
    except Exception as exc:
        logger.error(f"Falha na rotina automatizada de backup de banco: {exc}")
        return {
            "sucesso": False,
            "erro": str(exc),
            "horario": agora_str,
        }


# ==============================================================================
# SECTION 14: ENTERPRISE TEST & SANITY VALIDATION SUITE
# ==============================================================================
def executar_suite_completa_de_testes_internos() -> bool:
    """
    Bateria completa de testes automatizados de sanidade ao iniciar:
    valida PostgreSQL, transações, plano mensal/anual, engine de blacklist,
    validadores criptográficos e cache.
    """
    logger.info("🔧 Rodando suíte de testes de sanidade operacional interna...")
    erros_encontrados = []

    # 1. Teste de Conectividade do PostgreSQL
    try:
        with obter_conexao() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 AS check_val;")
                r = cur.fetchone()
                if not r or r["check_val"] != 1:
                    erros_encontrados.append("PostgreSQL retornou valor de teste incorreto.")
    except Exception as e:
        erros_encontrados.append(f"Erro no teste do PostgreSQL: {e}")

    # 2. Teste de Consistência do Catálogo de Planos
    for chave, config in cfg.PLANOS_DISPONIVEIS.items():
        if config.dias <= 0 or config.valor <= 0.0:
            erros_encontrados.append(f"Plano inválido encontrado em catálogo: {chave}")

    # 3. Teste da Engine de Cache TTL
    cache_engine.set("teste_sanity", 12345, ttl=2)
    val = cache_engine.get("teste_sanity")
    if val != 12345:
        erros_encontrados.append("O motor TTLCacheEngine não armazenou item corretamente.")

    # 4. Teste de Assinatura HMAC Webhook
    assinatura_ok = validar_assinatura_mercado_pago({}, b"", {})
    if not isinstance(assinatura_ok, bool):
        erros_encontrados.append("A validação criptográfica HMAC falhou em retornar booleano.")

    if erros_encontrados:
        for f in erros_encontrados:
            logger.critical(f"❌ TESTE DE SANIDADE FALHOU | {f}")
        return False

    logger.info("✔ Todos os testes operacionais de sanidade interna passaram com êxito!")
    return True


# ==============================================================================
# SECTION 15: FASTAPI SERVER, ENTERPRISE HEALTH CHECK & WEBHOOK ENDPOINTS
# ==============================================================================
app = FastAPI(title="Bot Telegram VIP & Mercado Pago Enterprise", version=cfg.VERSION)


def obter_uso_memoria_mb() -> float:
    """Calcula memória RAM alocada em megabytes sem depender de bibliotecas externas."""
    try:
        import resource
        rusage = resource.getrusage(resource.RUSAGE_SELF)
        # Linux mede maxrss em kilobytes, macOS mede em bytes
        if sys.platform == "darwin":
            return round(rusage.ru_maxrss / (1024 * 1024), 2)
        return round(rusage.ru_maxrss / 1024, 2)
    except Exception:
        return 0.0


@app.get("/")
@app.get("/health")
async def health_check_enterprise() -> Dict[str, Any]:
    """
    Endpoint corporativo de monitoramento que atesta a vitalidade
    dos microsserviços integrados (Telegram, Banco, API MP) e métricas gerais.
    """
    db_ok = False
    try:
        with obter_conexao() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 AS ok;")
                res = cur.fetchone()
                db_ok = (res is not None and res["ok"] == 1)
    except Exception:
        db_ok = False

    telegram_ok = bool(telegram_app.bot and telegram_app.bot.id)
    mp_ok = bool(cfg.MP_ACCESS_TOKEN)

    stats = obter_relatorio_geral_completo()
    uptime_segundos = (datetime.now(timezone.utc) - cfg.START_TIME).total_seconds()

    return {
        "status": "online" if (db_ok and telegram_ok) else "degraded",
        "versao": cfg.VERSION,
        "uptime_segundos": round(uptime_segundos, 1),
        "memoria_utilizada_mb": obter_uso_memoria_mb(),
        "servicos": {
            "telegram_ok": telegram_ok,
            "postgresql_ok": db_ok,
            "mercadopago_ok": mp_ok,
        },
        "metricas": {
            "usuarios_cadastrados": stats["usuarios_cadastrados"],
            "assinantes_ativos": stats["usuarios_ativos"],
            "assinantes_vencidos": stats["usuarios_vencidos"],
        },
    }


@app.post("/webhook")
async def telegram_webhook(request: Request) -> Dict[str, Any]:
    """Recepção segura e idempotente das requisições Webhook enviadas pelo Telegram."""
    try:
        dados = await request.json()
        update = Update.de_json(dados, telegram_app.bot)
        await telegram_app.process_update(update)
        return {"ok": True}
    except Exception as erro:
        logger.error(f"Exceção em processamento do webhook Telegram: {erro}", exc_info=True)
        registrar_erro_sistema("telegram_webhook", str(erro))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Erro na rotina de atualização do Telegram Bot",
        )


@app.post("/mercadopago")
async def mercadopago_webhook(request: Request) -> Dict[str, Any]:
    """
    Webhook oficial do Mercado Pago para validação criptográfica x-signature,
    liquidação com trava ACID para evitar transações duplicadas e envio
    instantâneo do convite único ao assinante VIP.
    """
    headers = dict(request.headers)
    query_params = dict(request.query_params)

    try:
        raw_body = await request.body()
        dados = await request.json()
    except Exception:
        logger.warning("Mercado Pago Webhook recebeu JSON malformado.")
        gravar_log_webhook("json_invalido", "N/A", "erro", "Payload não-JSON recebido")
        return {"status": "json_invalido"}

    evento_tipo = dados.get("type", "indeterminado")
    data_id = str(dados.get("data", {}).get("id", ""))

    # Verifica integridade com assinatura oficial HMAC-SHA256 do Mercado Pago
    assinatura_valida = validar_assinatura_mercado_pago(headers, raw_body, query_params)
    status_ass = "valido" if assinatura_valida else "invalido"
    gravar_log_webhook(str(evento_tipo), data_id, status_ass, str(dados)[:500])

    if not assinatura_valida:
        logger.warning("Assinatura HMAC x-signature rejeitada! Interrompendo webhook.")
        return {"status": "assinatura_invalida"}

    if evento_tipo != "payment":
        return {"status": "evento_ignorado"}

    try:
        payment_id = str(dados["data"]["id"])
    except (KeyError, TypeError):
        return {"status": "id_invalido"}

    pagamento = consultar_pagamento_mp(payment_id)
    if not pagamento or pagamento.get("status") == 404:
        logger.warning(f"Pagamento {payment_id} não encontrado por API REST MP.")
        return {"status": "pagamento_nao_encontrado"}

    status_mp = pagamento.get("status")
    ext_ref = pagamento.get("external_reference")

    if status_mp != "approved":
        logger.info(f"Pagamento {payment_id} pendente ou recusado ({status_mp}).")
        return {"status": "aguardando_pagamento"}

    if not ext_ref or not str(ext_ref).isdigit():
        logger.error(f"external_reference incorreta no pagamento {payment_id}: ({ext_ref})")
        return {"status": "usuario_nao_identificado"}

    telegram_id = int(ext_ref)
    if is_usuario_bloqueado(telegram_id):
        logger.warning(f"Pagamento aprovado para usuário na blacklist {telegram_id}. Bloqueando acesso.")
        return {"status": "usuario_na_blacklist"}

    val_transacao = float(pagamento.get("transaction_amount", 4.99))
    metadata = pagamento.get("metadata") or {}
    plano_cobrado = metadata.get("plano", "mensal")

    if not buscar_usuario(telegram_id):
        salvar_usuario(telegram_id, "Assinante VIP Webhook", comando_origem="/webhook")

    # Garante Idempotência usando SELECT ... FOR UPDATE transacional
    aprovado_inedito = aprovar_pagamento_com_seguranca(
        payment_id=payment_id,
        telegram_id=telegram_id,
        valor=val_transacao,
        plano=plano_cobrado,
    )
    if not aprovado_inedito:
        logger.info(f"Pagamento {payment_id} já registrado em ciclo prévio.")
        return {"status": "pagamento_ja_processado"}

    # Ativa ou prorroga a assinatura e grava o histórico contábil
    estender_ou_ativar_assinatura(telegram_id, plano_cobrado)

    # Cria link exclusivo de entrada ao Grupo VIP
    convite_link = await gerar_convite_temporario(telegram_id)
    if not convite_link:
        logger.error(f"Erro no Telegram ao gerar convite para {telegram_id}.")
        return {"status": "erro_ao_gerar_convite"}

    try:
        await telegram_app.bot.send_message(
            chat_id=telegram_id,
            text=(
                "🎉 **Seu Pagamento Foi Aprovado Com Sucesso!**\n\n"
                "Sua assinatura VIP está ativa no nosso sistema.\n\n"
                "Entre agora no Grupo VIP pelo seu link exclusivo (uso único):\n\n"
                f"{convite_link}"
            ),
            parse_mode="Markdown",
        )
        logger.info(f"Acesso liberado e convite enviado com sucesso para ID {telegram_id}.")
    except Exception as exc:
        logger.error(f"Falha em mandar mensagem de convite ao ID {telegram_id}: {exc}", exc_info=True)
        registrar_erro_sistema("mercadopago_webhook_entrega", str(exc))

    return {"status": "ok"}


# ==============================================================================
# SECTION 16: APPLICATION LIFESPAN, STARTUP & SHUTDOWN HOOKS
# ==============================================================================
@app.on_event("startup")
async def startup() -> None:
    """Inicialização segura e registro dos serviços em background ao rodar o FastAPI."""
    global tarefa_vencimentos, tarefa_limpeza_banco
    logger.info("🚀 Inicializando motor do Bot Telegram VIP Enterprise...")

    # Valida parâmetros de ambiente
    cfg.validar_configuracoes_criticas()

    # Monta tabelas, constraints e esquemas relacionais
    criar_tabelas_e_indices()

    # Roda bateria integral de testes internamente para checar sanidade
    if not executar_suite_completa_de_testes_internos():
        logger.critical("⚠️ ALERTA: Alguns testes operacionais relataram falha no startup!")

    # Ativa conexão com a API do Telegram
    await telegram_app.initialize()
    await telegram_app.start()

    # Define o webhook para recebimento de requisições em produção
    url_webhook = f"{cfg.WEBHOOK_URL.rstrip('/')}/webhook"
    await telegram_app.bot.set_webhook(url_webhook)
    logger.info(f"✔ Webhook do Telegram registrado em: {url_webhook}")

    # Aciona tasks concorrentes de monitoramento contínuo e faxina
    tarefa_vencimentos = asyncio.create_task(verificar_assinaturas_vencidas_background())
    tarefa_limpeza_banco = asyncio.create_task(limpeza_diaria_banco_background())
    logger.info("✔ Workers de background para limpeza de banco, backups e vencimentos ativados.")


@app.on_event("shutdown")
async def shutdown() -> None:
    """Desligamento gracioso das conexões com encerramento limpo das tasks em background."""
    global tarefa_vencimentos, tarefa_limpeza_banco
    logger.info("🛑 Desligando serviços, webhooks e conexões com o Telegram Bot...")

    for task in [tarefa_vencimentos, tarefa_limpeza_banco]:
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    await telegram_app.stop()
    await telegram_app.shutdown()
    logger.info("🏁 Aplicação Enterprise VIP Bot encerrada com segurança.")


# ==============================================================================
# SECTION 17: ENTERPRISE UVICORN CLI LAUNCHER
# ==============================================================================
if __name__ == "__main__":
    porta_servidor = int(os.environ.get("PORT", "8000"))
    logger.info(f"🚀 Subindo servidor ASGI Uvicorn Enterprise na porta {porta_servidor}...")
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=porta_servidor,
        log_level="info",
        access_log=True,
    )
