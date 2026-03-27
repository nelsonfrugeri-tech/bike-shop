# Bike Shop - Documentação Técnica

Bem-vindo à documentação oficial do projeto **Bike Shop** - Sistema multi-agent para monitoramento e análise automatizada de fundos de investimento.

## 📋 Documentação Disponível

### [🔧 Especificação Técnica](technical-specification.md)
Documentação completa da arquitetura, stack tecnológico, e decisões de design aprovadas pela equipe técnica.

**Highlights:**
- ✅ Arquitetura de 3 agents aprovada
- ✅ APIs oficiais validadas (CVM, BCB, ANBIMA, B3)
- ✅ Stack: Python + Claude API + Slack + SQLite
- ✅ Resilience layer com circuit breakers
- 🚀 **Ready for Implementation**

---

## 📊 Status do Projeto

| Componente | Status | Responsável |
|------------|--------|-------------|
| Arquitetura | ✅ Aprovada | Mr. Robot + Elliot |
| APIs | ✅ Validadas | Elliot |
| PRD | ✅ Finalizado | Tyrell |
| Implementação | 🟡 Aguardando Go | Team |

---

## 🏗️ Arquitetura Overview

```
Data Agent → Analysis Agent → Notification Agent
     ↓             ↓              ↓
  SQLite      Claude API      Slack
```

**Foco:** Fundo Nu Reserva Planejada FIF da CIC RF CP RL
**Objetivo:** Monitoramento inteligente com alertas automatizados

---

## 🚀 Quick Start

```bash
# Clone do repositório
git clone https://github.com/nelsonfrugeri-tech/bike-shop.git

# Setup ambiente
cd bike-shop
python -m venv .venv
source .venv/bin/activate

# Instalar dependências
uv sync

# Configurar environment
cp .env.example .env
# Editar .env com suas API keys

# Executar
python -m src.main
```

---

## 📞 Contato

**Equipe Técnica:**
- 🤖 **Mr. Robot** - Arquiteto Senior
- 💻 **Elliot Alderson** - Dev Principal
- 📈 **Tyrell Wellick** - Tech PM

---

*Última atualização: 27 de Março de 2026*