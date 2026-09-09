import uuid
import enum
from sqlalchemy import Column, String, Integer, BigInteger, ForeignKey, UniqueConstraint, Index, Enum
from sqlalchemy.dialects.postgresql import UUID, JSONB, TIMESTAMP
from app.core.db import Base

# ==========================================
# 1. Definizione degli ENUM
# ==========================================
class EnvEnum(enum.Enum):
    DEV = "DEV"
    UAT = "UAT"
    PROD = "PROD"
    # Aggiungi qui gli altri ambienti di PagoPA

class TriggerTypeEnum(enum.Enum):
    MANUAL = "MANUAL"
    CRON = "CRON"
    CI_PIPELINE = "CI_PIPELINE"

class ScenarioStatusEnum(enum.Enum):
    PASSED = "PASSED"
    FAILED = "FAILED"
    BROKEN = "BROKEN"
    SKIPPED = "SKIPPED"

# ==========================================
# 2. Modelli delle Tabelle
# ==========================================
class TestSuite(Base):
    __tablename__ = 'test_suites'
    __table_args__ = (
        # Vincolo di unicità composito per evitare anagrafiche duplicate
        UniqueConstraint('test_object', 'test_type', 'suite_version', name='uix_qachub_suite_def'),
        {'schema': 'qachub'}
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    test_object = Column(String, nullable=False) # NN
    test_type = Column(String, nullable=False)   # NN
    suite_version = Column(String, nullable=False) # NN
    owner_team = Column(String, nullable=True)

class TestRun(Base):
    __tablename__ = 'test_runs'
    __table_args__ = (
        # Indici per velocizzare le query sulle dashboard
        Index('ix_qachub_test_runs_timestamp_start', 'timestamp_start'),
        Index('ix_qachub_test_runs_env', 'env'),
        {'schema': 'qachub'}
    )

    # Convertito in UUID come da tua indicazione
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4) 
    
    # FK verso test_suites (con indice)
    suite_id = Column(UUID(as_uuid=True), ForeignKey('qachub.test_suites.id', ondelete="CASCADE"), nullable=False, index=True)

    scenario_qty = Column(Integer)
    passed_scenario = Column(Integer)
    failed_scenario = Column(Integer)
    broken_scenario = Column(Integer)

    # timestamptz in Postgres
    timestamp_start = Column(TIMESTAMP(timezone=True)) 
    timestamp_end = Column(TIMESTAMP(timezone=True))
    duration_ms = Column(BigInteger)

    # Tipi ENUM mappati su database nello schema corretto
    env = Column(Enum(EnvEnum, name="env_enum", schema="qachub"))
    trigger_type = Column(Enum(TriggerTypeEnum, name="trigger_type_enum", schema="qachub"))
    
    test_version = Column(String)

class TestExecution(Base):
    __tablename__ = 'test_executions'
    __table_args__ = (
        Index('ix_qachub_test_executions_status', 'status'),
        {'schema': 'qachub'}
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    
    # FK aggiornata a UUID per matchare TestRun.id
    run_id = Column(UUID(as_uuid=True), ForeignKey('qachub.test_runs.id', ondelete="CASCADE"), nullable=False, index=True)

    allure_id = Column(String)
    status = Column(Enum(ScenarioStatusEnum, name="scenario_status_enum", schema="qachub"))
    scenario_name = Column(String)
    allure_report = Column(JSONB)
    duration_ms = Column(BigInteger)
    error_message = Column(String)
    retries = Column(Integer)