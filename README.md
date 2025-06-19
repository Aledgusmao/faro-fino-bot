# Faro Fino - Bot de Monitoramento de Notícias v2.7

Um bot inteligente para monitoramento automático de notícias com **descoberta de links específicos** e **comando de verificação imediata**.

## 🆕 Novidades da Versão 2.7

### 🎯 **PRINCIPAL: Links Específicos de Notícias**
- **🔍 Descoberta automática**: O bot agora encontra links específicos de notícias
- **📰 Monitoramento individual**: Cada notícia é verificada separadamente
- **🔗 Link direto**: Retorna o link exato da notícia onde encontrou a palavra-chave
- **📂 Múltiplas seções**: Monitora página principal + política + economia

### ⚡ **Comando de Verificação Imediata**
- **`/verificar`**: Executa monitoramento imediatamente sem esperar 5 minutos
- **🚀 Teste instantâneo**: Perfeito para testar palavras-chave novas
- **📊 Feedback em tempo real**: Mostra quantos alertas foram encontrados

### 🔧 **Melhorias Técnicas Avançadas**
- **📡 Descoberta inteligente**: Extrai automaticamente links de notícias das páginas
- **🎯 Filtros precisos**: Identifica URLs válidas de notícias por padrões
- **📅 Extração de metadados**: Título, data de publicação e seção da notícia
- **🗂️ Histórico com seções**: Rastreia política, economia e geral separadamente
- **⚡ Performance otimizada**: Processamento paralelo e timeouts adequados

## 📱 Formato das Notificações v2.7

```
🚨 Palavras encontradas: STF, Alexandre de Moraes
📅 Publicado: 19/06/2025
🗞️ G1 - Política

📰 STF decide sobre regulamentação das redes sociais

🔗 https://g1.globo.com/politica/noticia/2025/06/19/stf-decide-regulamentacao.ghtml

⏰ Detectado em: 19/06/2025 às 15:30
```

**🎯 Agora você clica no link e vai DIRETO para a notícia específica!**

## 🤖 Comandos Disponíveis

### Comandos Principais
- `/start` - Configurar bot e receber boas-vindas
- `/help` - Exibir menu de ajuda completo
- `/monitoramento` - Ativar/desativar monitoramento automático
- **`/verificar`** - **NOVO!** Executar monitoramento imediatamente
- `/status` - Ver status detalhado do monitoramento

### Gerenciamento de Conteúdo
- `/verpalavras` - Listar palavras-chave cadastradas
- `/verperfis` - Listar fontes e perfis cadastrados
- `/limparhistorico` - Limpar histórico de links notificados

### Adição e Remoção via Mensagens
- `@termo1, termo2` - Adicionar palavras-chave
- `@https://site.com` - Adicionar site para monitoramento
- `#termo1, termo2` - Remover palavras-chave ou fontes

## 🔧 Configuração e Instalação

### Pré-requisitos
- Python 3.8+
- Token do bot do Telegram (obtido via @BotFather)
- Conta no Railway, Heroku ou similar para hospedagem

### Dependências
```
python-telegram-bot==20.7
httpx==0.25.2
beautifulsoup4==4.12.2
```

### Variáveis de Ambiente
- `BOT_TOKEN` - Token do bot do Telegram

### Instalação Local
```bash
# Clonar ou baixar os arquivos
cd faro_fino_bot_v2_7

# Instalar dependências
pip install -r requirements.txt

# Configurar variável de ambiente
export BOT_TOKEN="seu_token_aqui"

# Executar o bot
python bot.py
```

### Deploy no Railway
1. Faça upload dos arquivos para um repositório GitHub
2. Conecte o repositório ao Railway
3. Configure a variável de ambiente `BOT_TOKEN`
4. O deploy será automático

## 📊 Funcionalidades Técnicas v2.7

### Descoberta de Links Específicos
- **Múltiplas seções**: Página principal + /politica/ + /economia/
- **Filtros inteligentes**: Identifica URLs de notícias por padrões
- **Limite configurável**: Máximo 20 links por página (configurável)
- **Domínios suportados**: G1, Oeste e outros sites de notícias

### Monitoramento Individual
- **Extração de metadados**: Título, data de publicação, texto limpo
- **Verificação de palavras-chave**: Busca por palavras completas
- **Filtragem temporal**: Apenas notícias dos últimos 7 dias
- **Prevenção de duplicatas**: Histórico inteligente por link específico

### Performance e Escalabilidade
- **Processamento otimizado**: Timeouts de 15 segundos por requisição
- **Histórico limitado**: Máximo 1.000 links com limpeza automática
- **Rate limiting**: Pausa de 1 segundo entre notificações
- **Logs detalhados**: Monitoramento completo do processo

## 🗂️ Estrutura de Dados v2.7

```json
{
  "telegram_owner_id": 123456789,
  "palavras_chave": ["STF", "economia"],
  "sites_monitorados": ["https://g1.globo.com"],
  "monitoramento_ativo": true,
  "ultima_verificacao": "2025-06-19T15:30:00",
  "historico_links": {
    "https://g1.globo.com/politica/noticia/2025/06/19/stf.html": {
      "data_notificacao": "2025-06-19T15:30:00",
      "data_publicacao": "2025-06-19",
      "secao": "Política"
    }
  },
  "configuracao_avancada": {
    "max_links_por_pagina": 20,
    "timeout_requisicao": 15,
    "dias_filtro_noticias": 7
  }
}
```

## 🛡️ Segurança

- **Proprietário único**: Apenas o primeiro usuário pode configurar e usar o bot
- **Verificação de acesso**: Todos os comandos verificam se o usuário é o proprietário
- **Configuração persistente**: Dados salvos em arquivo JSON local
- **Rate limiting**: Proteção contra spam e sobrecarga

## 📝 Logs e Monitoramento v2.7

O bot gera logs detalhados para facilitar o debug:
- `🔍 Iniciando monitoramento de X sites para Y palavras-chave`
- `📂 Descobrindo links em política: https://g1.globo.com/politica/`
- `✅ 15 links descobertos`
- `📊 Monitoramento concluído: 45 links descobertos, 3 alertas gerados`
- `📢 3 alertas enviados`

## 🔄 Atualizações

### Da v2.6 para v2.7
1. Substitua todos os arquivos do projeto pela v2.7
2. Faça commit e push para o GitHub
3. O Railway fará o deploy automático
4. **Teste o comando `/verificar`** para verificação imediata
5. **Observe os links específicos** nas notificações

### Principais Diferenças
- ✅ **Links específicos** em vez de páginas principais
- ✅ **Comando `/verificar`** para execução imediata
- ✅ **Seções identificadas** (Política, Economia, Geral)
- ✅ **Descoberta automática** de links de notícias
- ✅ **Performance otimizada** para múltiplas requisições

## 🆘 Suporte

### Problemas Comuns
- **Bot não responde**: Verifique se o `BOT_TOKEN` está configurado corretamente
- **Monitoramento não funciona**: Use `/status` para verificar se está ativo
- **Comando `/verificar` não funciona**: Certifique-se de ter palavras-chave e fontes configuradas
- **Links ainda genéricos**: Verifique se atualizou para v2.7 corretamente

### Teste da Nova Funcionalidade
1. Configure palavras-chave: `@STF, economia`
2. Configure fonte: `@https://g1.globo.com`
3. Ative monitoramento: `/monitoramento`
4. **Teste imediatamente**: `/verificar`
5. **Observe o link específico** na notificação

## 📄 Licença

Este projeto é de uso pessoal e educacional.

## 🏷️ Versão

**v2.7** - Junho 2025
- **Descoberta de links específicos de notícias**
- **Comando `/verificar` para execução imediata**
- **Monitoramento de múltiplas seções (principal + política + economia)**
- **Links diretos para notícias individuais**
- **Performance otimizada e logs detalhados**

---

### 🎯 **Resultado Final**
**Agora você recebe o link EXATO da notícia onde a palavra-chave foi encontrada, podendo acessar diretamente o conteúdo relevante!**

