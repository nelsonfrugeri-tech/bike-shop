# Bike Shop - Especificação Técnica

**Data:** 27 de Março de 2026
**Status:** Arquitetura Final Aprovada
**Versão:** 1.0

## Visão Geral

Sistema multi-agent para monitoramento e análise automatizada do fundo **Nu Reserva Planejada FIF da CIC RF CP RL** (CNPJ 43.121.002/0001-41) com notificação inteligente via Slack.

## Arquitetura Final

### Stack Tecnológico Aprovado

- **Linguagem:** Python 3.11+
- **Scheduler:** Sistema de agendamento (cron-like)
- **AI Engine:** Claude API (Anthropic)
- **Storage:** SQLite (local)
- **Notificação:** Slack
- **APIs:** Fontes oficiais apenas

### Componentes da Arquitetura

```
┌─────────────────┐    ┌─────────────────┐    ┌─────────────────┐
│   Data Agent    │───▶│ Analysis Agent  │───▶│ Notification    │
│                 │    │                 │    │    Agent        │
│ • CVM API       │    │ • Claude API    │    │ • Slack         │
│ • BCB API       │    │ • Data Analysis │    │ • Alertas       │
│ • ANBIMA API    │    │ • Pattern Rec.  │    │ • Reports       │
│ • B3 API        │    │ • Trend Analysis│    │                 │
└─────────────────┘    └─────────────────┘    └─────────────────┘
          │                        │                        │
          ▼                        ▼                        ▼
   ┌─────────────┐        ┌─────────────┐        ┌─────────────┐
   │ SQLite DB   │        │ Context     │        │ Slack       │
   │ • Raw data  │        │ Storage     │        │ Channel     │
   │ • Cache     │        │ • Analysis  │        │ • History   │
   └─────────────┘        │ • Insights  │        │ • Logs      │
                          └─────────────┘        └─────────────┘
```

## 1. Data Agent - Coleta de Dados

### Responsabilidades
- Coleta dados do fundo Nu Reserva via APIs oficiais
- Implementa circuit breakers e fallbacks
- Cache inteligente com warm-up
- Validação e normalização de dados

### Fontes de Dados Validadas

#### 1. ANBIMA API v2 (Primary)
- **URL:** `https://api.anbima.com.br/feed/fundos/v2/fundos/{codigo}`
- **Status:** ✅ Validada - API oficial mais robusta
- **Dados:** Performance, patrimônio, rentabilidade

#### 2. CVM Portal Dados Abertos
- **Formato:** CSV downloads (sem API REST oficial)
- **Status:** ✅ Validada - fonte governamental oficial
- **Dados:** Informações regulamentares, composição da carteira

#### 3. BCB API (via python-bcb)
- **Lib:** `python-bcb` (MIT, 52k downloads/mês)
- **Status:** ✅ Aprovada - biblioteca oficial
- **Dados:** Indicadores econômicos, taxa Selic, CDI

#### 4. B3 API
- **Status:** ✅ Validada - implementação própria
- **Dados:** Dados de mercado, benchmarks

### Resilience Layer

#### Circuit Breakers
```python
class CircuitBreaker:
    def __init__(self, timeout: int = 3, failure_threshold: int = 3):
        self.timeout = timeout
        self.failure_threshold = failure_threshold
        self.failure_count = 0
        self.state = "CLOSED"  # CLOSED, OPEN, HALF_OPEN
```

#### Fallback Chain
```
CVM API (primary) → ANBIMA API → Cache → Error Notification
```

#### Timeouts e Retry Logic
- **Health Check:** 3s timeout
- **API Calls:** 10s timeout
- **Retry:** Exponential backoff (1s, 2s, 4s)
- **Circuit Open:** 5 minutos

### Warm-up e Cache Strategy

```python
async def __init__(self):
    """DataAgent initialization with warm-up cache"""
    await self.warm_up_cache()
    await self.health_check_endpoints()
```

## 2. Analysis Agent - Processamento Inteligente

### Responsabilidades
- Análise de dados via Claude API
- Detecção de padrões e tendências
- Geração de insights personalizados
- Contextualização com mercado

### Claude API Integration
```python
class AnalysisAgent:
    def __init__(self):
        self.claude_client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    async def analyze_fund_performance(self, data: FundData) -> AnalysisResult:
        prompt = self.build_analysis_prompt(data)
        response = await self.claude_client.messages.create(...)
        return self.parse_analysis(response)
```

### Análises Implementadas
- **Performance vs CDI/Selic**
- **Análise de volatilidade**
- **Comparação com peers**
- **Detecção de anomalias**
- **Projeções e tendências**

## 3. Notification Agent - Comunicação

### Responsabilidades
- Formatação de alertas para Slack
- Gestão de frequência de notificações
- Histórico e logs de comunicação
- Escalation rules

### Slack Integration
```python
class NotificationAgent:
    def __init__(self):
        self.slack_client = WebClient(token=os.getenv("SLACK_BOT_TOKEN"))

    async def send_analysis(self, analysis: AnalysisResult):
        formatted_message = self.format_slack_message(analysis)
        await self.slack_client.chat_postMessage(...)
```

## Storage e Persistência

### SQLite Schema
```sql
-- Dados brutos do fundo
CREATE TABLE fund_data (
    id INTEGER PRIMARY KEY,
    timestamp DATETIME,
    fonte VARCHAR(50),
    patrimonio_liquido DECIMAL,
    rentabilidade_dia DECIMAL,
    rentabilidade_mes DECIMAL,
    rentabilidade_ano DECIMAL,
    cota_valor DECIMAL,
    raw_data JSON
);

-- Análises geradas
CREATE TABLE analyses (
    id INTEGER PRIMARY KEY,
    timestamp DATETIME,
    data_ids TEXT,  -- JSON array of fund_data.id
    analysis_text TEXT,
    insights JSON,
    alert_level VARCHAR(20)
);

-- Cache de health checks
CREATE TABLE api_health (
    endpoint VARCHAR(100) PRIMARY KEY,
    last_check DATETIME,
    status VARCHAR(20),
    response_time_ms INTEGER,
    error_message TEXT
);
```

## Cronograma de Execução

### Frequência de Coleta
- **Dados principais:** A cada 4 horas (6x/dia)
- **Health checks:** A cada hora
- **Análise completa:** Diária (22h)
- **Relatório semanal:** Sextas 18h

### Scheduler Configuration
```python
# Agendamento usando APScheduler
scheduler.add_job(
    func=data_agent.collect_fund_data,
    trigger="interval",
    hours=4,
    id="fund_data_collection"
)

scheduler.add_job(
    func=analysis_agent.daily_analysis,
    trigger="cron",
    hour=22,
    minute=0,
    id="daily_analysis"
)
```

## Configuração e Deploy

### Variáveis de Ambiente
```bash
# APIs
ANTHROPIC_API_KEY=sk-...
SLACK_BOT_TOKEN=xoxb-...
SLACK_CHANNEL=C...

# Database
DATABASE_PATH=./data/bike_shop.db

# Circuit Breakers
API_TIMEOUT=10
HEALTH_CHECK_TIMEOUT=3
MAX_RETRY_ATTEMPTS=3
CIRCUIT_FAILURE_THRESHOLD=5

# Scheduler
COLLECTION_INTERVAL_HOURS=4
ANALYSIS_HOUR=22
```

### Dependências Python
```toml
[dependencies]
python-bcb = "^0.1.0"
anthropic = "^0.21.0"
slack-sdk = "^3.27.0"
aiohttp = "^3.9.0"
apscheduler = "^3.10.0"
pydantic = "^2.6.0"
sqlalchemy = "^2.0.0"
```

## Monitoramento e Observabilidade

### Logs Estruturados
```python
import structlog

logger = structlog.get_logger()

# Exemplo de uso
logger.info(
    "fund_data_collected",
    fonte="anbima",
    patrimonio=1250000.50,
    response_time_ms=245,
    circuit_state="closed"
)
```

### Health Checks
```
GET /health/data-agent     → Status do Data Agent
GET /health/analysis-agent → Status do Analysis Agent
GET /health/notification   → Status do Notification Agent
GET /health/database      → Status do SQLite
GET /health/apis          → Status de todas as APIs externas
```

### Alertas de Sistema
- **API Down:** > 3 falhas consecutivas
- **Database Error:** Falha ao escrever dados
- **Analysis Failure:** Claude API indisponível
- **Slack Notification Failed:** Canal inacessível

## Decisões Arquiteturais

### Por que SQLite?
✅ **Simplicidade** - Zero configuração de servidor
✅ **Performance** - Adequada para volume de dados
✅ **Backup** - Arquivo único, fácil backup
✅ **Local** - Sem dependência de serviços externos

### Por que Claude API?
✅ **Context Length** - Suporta análises complexas
✅ **Reasoning** - Capacidade analítica superior
✅ **Structured Output** - JSON bem formatado
✅ **Cost-Effective** - Preço por token competitivo

### Por que Async Python?
✅ **Concurrency** - Múltiplas APIs simultaneamente
✅ **Non-blocking** - UI responsiva durante coleta
✅ **Efficiency** - Melhor uso de recursos
✅ **Modern** - Padrão atual Python 3.11+

### Por que Não Scraping?
❌ **Fragilidade** - Quebra com mudanças de layout
❌ **Legal** - Possíveis questões de ToS
❌ **Maintenance** - Alto overhead de manutenção
✅ **APIs Oficiais** - Mais confiáveis e estáveis

## Segurança

### API Keys
- Variáveis de ambiente apenas
- Rotação periódica recomendada
- Princípio do menor privilégio

### Data Privacy
- Dados financeiros apenas localmente
- Sem exposição pública de dados
- Logs sem informações sensíveis

### Error Handling
```python
try:
    fund_data = await api_client.get_fund_data()
except APIException as e:
    logger.error("api_error", endpoint=e.endpoint, error=str(e))
    await notification_agent.alert_api_error(e)
    return cached_data
```

## Roadmap Futuro

### Phase 2 - Expansão
- [ ] Múltiplos fundos
- [ ] Comparações entre fundos
- [ ] API REST para terceiros
- [ ] Dashboard web

### Phase 3 - Analytics Avançado
- [ ] Machine Learning predictions
- [ ] Sentiment analysis de notícias
- [ ] Portfolio optimization
- [ ] Risk analysis

---

## Validação Final

**✅ Arquitetura Aprovada** - Mr. Robot + Elliot Alderson (27/03/2026)
**✅ APIs Validadas** - Fundo Nu Reserva acessível
**✅ Stack Confirmado** - Python + Claude + Slack + SQLite
**✅ Resilience Layer** - Circuit breakers + fallbacks
**🚀 Ready for Implementation** - Aguardando liberação final

---

*Documentação gerada automaticamente por **Elliot Alderson** baseada nas decisões arquiteturais aprovadas pela equipe técnica.*