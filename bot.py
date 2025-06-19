import os
import json
import logging
import asyncio
import httpx
from datetime import datetime, timedelta
from telegram import Update, Bot
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
from bs4 import BeautifulSoup
import re
from urllib.parse import urlparse, urljoin

# Configuração de logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

CONFIG_PATH = "faro_fino_config.json"
MONITORAMENTO_INTERVAL = 300  # 5 minutos em segundos
DIAS_FILTRO_NOTICIAS = 7  # Considerar apenas notícias dos últimos 7 dias
DIAS_HISTORICO_LINKS = 30  # Manter histórico de links por 30 dias
MAX_LINKS_HISTORICO = 1000  # Máximo de links no histórico
MAX_LINKS_POR_PAGINA = 20  # Máximo de links de notícias por página

# Mapeamento de seções por site
SECOES_SITES = {
    "g1.globo.com": {
        "principal": "https://g1.globo.com",
        "politica": "https://g1.globo.com/politica/",
        "economia": "https://g1.globo.com/economia/"
    },
    "oeste.com.br": {
        "principal": "https://www.oeste.com.br",
        "politica": "https://www.oeste.com.br/politica/",
        "economia": "https://www.oeste.com.br/economia/"
    }
}

# Configuração padrão
DEFAULT_CONFIG = {
    "telegram_owner_id": None,
    "palavras_chave": [],
    "sites_monitorados": [],
    "perfis_twitter": [],
    "perfis_instagram": [],
    "monitoramento_ativo": False,
    "ultima_verificacao": None,
    "historico_links": {},  # {url: {"data_notificacao": "2025-06-19T15:30:00", "data_publicacao": "2025-06-19", "secao": "politica"}}
    "configuracao_avancada": {
        "max_links_por_pagina": MAX_LINKS_POR_PAGINA,
        "timeout_requisicao": 15,
        "dias_filtro_noticias": DIAS_FILTRO_NOTICIAS
    }
}

def carregar_config():
    """Carrega a configuração do arquivo JSON ou cria uma nova com valores padrão."""
    try:
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                config = json.load(f)
                # Garantir que todas as chaves necessárias existam
                for key, value in DEFAULT_CONFIG.items():
                    if key not in config:
                        config[key] = value
                return config
        else:
            logger.info("Arquivo de configuração não encontrado. Criando configuração padrão.")
            return DEFAULT_CONFIG.copy()
    except Exception as e:
        logger.error(f"Erro ao carregar configuração: {e}")
        return DEFAULT_CONFIG.copy()

def salvar_config(config_data):
    """Salva a configuração no arquivo JSON."""
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(config_data, f, ensure_ascii=False, indent=4)
        logger.info("Configuração salva com sucesso.")
    except Exception as e:
        logger.error(f"Erro ao salvar configuração: {e}")

def limpar_historico_antigo(config_data):
    """Remove links antigos do histórico baseado na data de notificação."""
    try:
        historico = config_data.get("historico_links", {})
        data_limite = datetime.now() - timedelta(days=DIAS_HISTORICO_LINKS)
        
        links_para_remover = []
        for url, dados in historico.items():
            try:
                data_notificacao = datetime.fromisoformat(dados.get("data_notificacao", ""))
                if data_notificacao < data_limite:
                    links_para_remover.append(url)
            except:
                # Se não conseguir parsear a data, remove o link
                links_para_remover.append(url)
        
        for url in links_para_remover:
            del historico[url]
        
        # Também limitar o número máximo de links
        if len(historico) > MAX_LINKS_HISTORICO:
            # Ordenar por data de notificação e manter apenas os mais recentes
            historico_ordenado = sorted(
                historico.items(),
                key=lambda x: x[1].get("data_notificacao", ""),
                reverse=True
            )
            historico_limitado = dict(historico_ordenado[:MAX_LINKS_HISTORICO])
            config_data["historico_links"] = historico_limitado
        
        if links_para_remover:
            logger.info(f"🧹 Removidos {len(links_para_remover)} links antigos do histórico")
            salvar_config(config_data)
            
    except Exception as e:
        logger.error(f"Erro ao limpar histórico: {e}")

def extrair_nome_fonte(url):
    """Extrai um nome amigável da fonte a partir da URL."""
    try:
        parsed = urlparse(url)
        domain = parsed.netloc.lower()
        
        # Mapeamento de domínios para nomes amigáveis
        mapeamento = {
            "g1.globo.com": "G1",
            "folha.uol.com.br": "FOLHA",
            "www.folha.uol.com.br": "FOLHA",
            "estadao.com.br": "ESTADÃO",
            "www.estadao.com.br": "ESTADÃO",
            "veja.abril.com.br": "VEJA",
            "www.veja.abril.com.br": "VEJA",
            "oglobo.globo.com": "O GLOBO",
            "www.oglobo.globo.com": "O GLOBO",
            "www1.folha.uol.com.br": "FOLHA",
            "noticias.uol.com.br": "UOL",
            "www.uol.com.br": "UOL",
            "www.cnn.com.br": "CNN BRASIL",
            "cnn.com.br": "CNN BRASIL",
            "www.oeste.com.br": "OESTE",
            "oeste.com.br": "OESTE"
        }
        
        if domain in mapeamento:
            return mapeamento[domain]
        
        # Se não estiver no mapeamento, tentar extrair o nome principal
        parts = domain.split('.')
        if len(parts) >= 2:
            if parts[0] == 'www':
                return parts[1].upper()
            else:
                return parts[0].upper()
        
        return domain.upper()
        
    except Exception as e:
        logger.error(f"Erro ao extrair nome da fonte de {url}: {e}")
        return "FONTE DESCONHECIDA"

def extrair_data_da_url(url):
    """Tenta extrair a data de publicação a partir da URL."""
    try:
        # Padrões comuns de data em URLs
        padroes = [
            r'/(\d{4})/(\d{1,2})/(\d{1,2})/',  # /2025/06/19/
            r'/(\d{4})-(\d{1,2})-(\d{1,2})/',  # /2025-06-19/
            r'/(\d{1,2})/(\d{1,2})/(\d{4})/',  # /19/06/2025/
            r'/(\d{1,2})-(\d{1,2})-(\d{4})/',  # /19-06-2025/
        ]
        
        for padrao in padroes:
            match = re.search(padrao, url)
            if match:
                grupos = match.groups()
                if len(grupos) == 3:
                    # Determinar qual é ano, mês e dia baseado no tamanho
                    if len(grupos[0]) == 4:  # Primeiro é ano
                        ano, mes, dia = grupos
                    else:  # Último é ano
                        dia, mes, ano = grupos
                    
                    try:
                        data = datetime(int(ano), int(mes), int(dia))
                        return data.date()
                    except ValueError:
                        continue
        
        return None
        
    except Exception as e:
        logger.error(f"Erro ao extrair data da URL {url}: {e}")
        return None

def eh_url_noticia(url, dominio):
    """Identifica se uma URL é de uma notícia específica."""
    try:
        parsed = urlparse(url)
        path = parsed.path.lower()
        
        # Padrões comuns de URLs de notícias
        padroes_noticia = [
            r'/noticia/',
            r'/\d{4}/\d{1,2}/\d{1,2}/',  # /2025/06/19/
            r'/artigo/',
            r'/post/',
            r'/materia/',
            r'/reportagem/'
        ]
        
        # Verificar se contém algum padrão de notícia
        for padrao in padroes_noticia:
            if re.search(padrao, path):
                return True
        
        # Para G1: URLs que terminam com .html ou .ghtml
        if dominio == "g1.globo.com" and (path.endswith('.html') or path.endswith('.ghtml')):
            return True
        
        # Para Oeste: URLs com estrutura de notícia
        if dominio == "oeste.com.br" and len(path.split('/')) >= 3:
            return True
            
        return False
        
    except Exception as e:
        logger.error(f"Erro ao verificar URL {url}: {e}")
        return False

def identificar_secao_url(url):
    """Identifica a seção de uma URL (política, economia, etc.)."""
    try:
        path = url.lower()
        if '/politica/' in path:
            return "Política"
        elif '/economia/' in path:
            return "Economia"
        else:
            return "Geral"
    except:
        return "Geral"

# Carregar configuração inicial
config_data = carregar_config()

class MonitoramentoManager:
    """Gerenciador do monitoramento automático com descoberta de links específicos."""
    
    def __init__(self, bot_token):
        self.bot_token = bot_token
        self.bot = Bot(token=bot_token)
        self.monitoramento_task = None
        self.running = False
    
    async def descobrir_links_noticias(self, url_pagina, dominio, max_links=None):
        """Descobre links de notícias em uma página."""
        if max_links is None:
            max_links = MAX_LINKS_POR_PAGINA
            
        try:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
            }
            
            async with httpx.AsyncClient(timeout=15, headers=headers) as client:
                response = await client.get(url_pagina)
                if response.status_code == 200:
                    soup = BeautifulSoup(response.text, 'html.parser')
                    
                    # Encontrar todos os links
                    links = soup.find_all('a', href=True)
                    links_noticias = []
                    
                    for link in links:
                        href = link['href']
                        
                        # Converter para URL absoluta se necessário
                        if href.startswith('/'):
                            href = urljoin(url_pagina, href)
                        elif not href.startswith('http'):
                            continue
                        
                        # Verificar se é uma notícia
                        if eh_url_noticia(href, dominio):
                            # Evitar duplicatas
                            if href not in links_noticias:
                                links_noticias.append(href)
                                
                                # Limitar número de links
                                if len(links_noticias) >= max_links:
                                    break
                    
                    return links_noticias
                else:
                    logger.warning(f"Status {response.status_code} para {url_pagina}")
                    return []
                    
        except Exception as e:
            logger.error(f"Erro ao descobrir links em {url_pagina}: {e}")
            return []
    
    async def extrair_metadados_pagina(self, url):
        """Extrai título, data de publicação e texto de uma página."""
        try:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
            }
            
            async with httpx.AsyncClient(timeout=15, headers=headers) as client:
                response = await client.get(url)
                if response.status_code == 200:
                    soup = BeautifulSoup(response.text, 'html.parser')
                    
                    # Extrair título
                    titulo = ""
                    title_tag = soup.find('title')
                    if title_tag:
                        titulo = title_tag.get_text().strip()
                    
                    # Tentar extrair data de publicação de meta tags
                    data_publicacao = None
                    
                    # Procurar por meta tags de data
                    meta_tags_data = [
                        'article:published_time',
                        'datePublished',
                        'publishdate',
                        'date',
                        'DC.date.issued',
                        'publication_date'
                    ]
                    
                    for meta_name in meta_tags_data:
                        meta_tag = soup.find('meta', {'property': meta_name}) or soup.find('meta', {'name': meta_name})
                        if meta_tag and meta_tag.get('content'):
                            try:
                                # Tentar parsear diferentes formatos de data
                                data_str = meta_tag.get('content')
                                # Remover timezone info se presente
                                data_str = re.sub(r'[+-]\d{2}:\d{2}$', '', data_str)
                                data_str = re.sub(r'Z$', '', data_str)
                                
                                # Tentar diferentes formatos
                                formatos = [
                                    '%Y-%m-%dT%H:%M:%S',
                                    '%Y-%m-%d %H:%M:%S',
                                    '%Y-%m-%d',
                                    '%d/%m/%Y',
                                    '%d-%m-%Y'
                                ]
                                
                                for formato in formatos:
                                    try:
                                        data_publicacao = datetime.strptime(data_str, formato).date()
                                        break
                                    except ValueError:
                                        continue
                                
                                if data_publicacao:
                                    break
                                    
                            except Exception as e:
                                logger.debug(f"Erro ao parsear data de meta tag {meta_name}: {e}")
                                continue
                    
                    # Se não encontrou data nas meta tags, tentar extrair da URL
                    if not data_publicacao:
                        data_publicacao = extrair_data_da_url(url)
                    
                    # Extrair texto limpo
                    for script in soup(["script", "style"]):
                        script.decompose()
                    
                    texto = soup.get_text()
                    linhas = (linha.strip() for linha in texto.splitlines())
                    chunks = (frase.strip() for linha in linhas for frase in linha.split("  "))
                    texto_limpo = ' '.join(chunk for chunk in chunks if chunk)
                    
                    return {
                        'titulo': titulo,
                        'data_publicacao': data_publicacao,
                        'texto': texto_limpo.lower(),
                        'url': url
                    }
                else:
                    logger.warning(f"Status {response.status_code} para {url}")
                    return None
                    
        except Exception as e:
            logger.error(f"Erro ao extrair metadados de {url}: {e}")
            return None
    
    def eh_noticia_recente(self, data_publicacao):
        """Verifica se a notícia é recente (últimos X dias)."""
        if not data_publicacao:
            # Se não conseguiu extrair a data, considera como recente para não perder notícias
            return True
        
        data_limite = datetime.now().date() - timedelta(days=DIAS_FILTRO_NOTICIAS)
        return data_publicacao >= data_limite
    
    def ja_foi_notificado(self, url, config_data):
        """Verifica se o link já foi notificado anteriormente."""
        historico = config_data.get("historico_links", {})
        return url in historico
    
    def adicionar_ao_historico(self, url, data_publicacao, secao, config_data):
        """Adiciona um link ao histórico de notificações."""
        if "historico_links" not in config_data:
            config_data["historico_links"] = {}
        
        config_data["historico_links"][url] = {
            "data_notificacao": datetime.now().isoformat(),
            "data_publicacao": data_publicacao.isoformat() if data_publicacao else None,
            "secao": secao
        }
    
    async def verificar_palavras_chave(self, texto, palavras_chave):
        """Verifica se alguma palavra-chave está presente no texto."""
        encontradas = []
        for palavra in palavras_chave:
            # Busca por palavra completa (não apenas substring)
            padrao = r'\b' + re.escape(palavra.lower()) + r'\b'
            if re.search(padrao, texto):
                encontradas.append(palavra)
        return encontradas
    
    async def monitorar_noticia_especifica(self, url_noticia, palavras_chave, config_data):
        """Monitora uma notícia específica."""
        try:
            # Verificar se já foi notificado
            if self.ja_foi_notificado(url_noticia, config_data):
                logger.debug(f"Link já notificado: {url_noticia}")
                return None
            
            metadados = await self.extrair_metadados_pagina(url_noticia)
            if not metadados:
                return None
            
            # Verificar se é notícia recente
            if not self.eh_noticia_recente(metadados['data_publicacao']):
                logger.debug(f"Notícia antiga ignorada: {url_noticia}")
                return None
            
            # Verificar palavras-chave
            palavras_encontradas = await self.verificar_palavras_chave(metadados['texto'], palavras_chave)
            if palavras_encontradas:
                # Identificar seção
                secao = identificar_secao_url(url_noticia)
                
                # Adicionar ao histórico
                self.adicionar_ao_historico(url_noticia, metadados['data_publicacao'], secao, config_data)
                
                return {
                    'url': url_noticia,
                    'titulo': metadados['titulo'],
                    'data_publicacao': metadados['data_publicacao'],
                    'palavras': palavras_encontradas,
                    'timestamp': datetime.now().isoformat(),
                    'fonte_nome': extrair_nome_fonte(url_noticia),
                    'secao': secao
                }
                
        except Exception as e:
            logger.error(f"Erro ao monitorar notícia {url_noticia}: {e}")
        return None
    
    async def executar_monitoramento(self, executar_imediatamente=False):
        """Executa uma rodada de monitoramento com descoberta de links específicos."""
        config = carregar_config()
        
        # Limpar histórico antigo
        limpar_historico_antigo(config)
        
        # Verificar se o monitoramento está ativo (exceto se for execução imediata)
        if not executar_imediatamente and not config.get("monitoramento_ativo", False):
            return []
        
        # Verificar se há proprietário configurado
        owner_id = config.get("telegram_owner_id")
        if not owner_id:
            logger.warning("Proprietário não configurado. Monitoramento pausado.")
            return []
        
        palavras_chave = config.get("palavras_chave", [])
        if not palavras_chave:
            logger.info("Nenhuma palavra-chave configurada.")
            return []
        
        # Coletar sites monitorados
        sites_monitorados = config.get("sites_monitorados", [])
        if not sites_monitorados:
            logger.info("Nenhum site configurado.")
            return []
        
        logger.info(f"🔍 Iniciando monitoramento de {len(sites_monitorados)} sites para {len(palavras_chave)} palavras-chave")
        
        resultados = []
        total_links_descobertos = 0
        
        # Para cada site monitorado
        for site_url in sites_monitorados:
            try:
                # Extrair domínio
                parsed = urlparse(site_url)
                dominio = parsed.netloc.lower()
                
                # Verificar se temos configuração de seções para este domínio
                if dominio not in SECOES_SITES:
                    logger.warning(f"Domínio {dominio} não configurado para descoberta de seções")
                    continue
                
                secoes = SECOES_SITES[dominio]
                
                # Para cada seção (principal, política, economia)
                for nome_secao, url_secao in secoes.items():
                    try:
                        logger.info(f"📂 Descobrindo links em {nome_secao}: {url_secao}")
                        
                        # Descobrir links de notícias
                        links_noticias = await self.descobrir_links_noticias(url_secao, dominio)
                        total_links_descobertos += len(links_noticias)
                        
                        logger.info(f"   ✅ {len(links_noticias)} links descobertos")
                        
                        # Monitorar cada link de notícia
                        for link_noticia in links_noticias:
                            resultado = await self.monitorar_noticia_especifica(link_noticia, palavras_chave, config)
                            if resultado:
                                resultados.append(resultado)
                                
                    except Exception as e:
                        logger.error(f"Erro ao processar seção {nome_secao} de {dominio}: {e}")
                        continue
                        
            except Exception as e:
                logger.error(f"Erro ao processar site {site_url}: {e}")
                continue
        
        logger.info(f"📊 Monitoramento concluído: {total_links_descobertos} links descobertos, {len(resultados)} alertas gerados")
        
        # Enviar notificações se houver resultados
        if resultados:
            await self.enviar_notificacoes(owner_id, resultados)
            logger.info(f"📢 {len(resultados)} alertas enviados")
            # Salvar configuração com histórico atualizado
            salvar_config(config)
        else:
            logger.info("✅ Nenhuma palavra-chave encontrada")
        
        # Atualizar timestamp da última verificação
        config["ultima_verificacao"] = datetime.now().isoformat()
        salvar_config(config)
        
        return resultados
    
    async def enviar_notificacoes(self, chat_id, resultados):
        """Envia notificações para o usuário."""
        try:
            for resultado in resultados:
                palavras_str = ", ".join(resultado['palavras'])
                
                # Formatar data de publicação
                data_publicacao_str = "Data não disponível"
                if resultado['data_publicacao']:
                    try:
                        if isinstance(resultado['data_publicacao'], str):
                            data_pub = datetime.fromisoformat(resultado['data_publicacao']).date()
                        else:
                            data_pub = resultado['data_publicacao']
                        data_publicacao_str = data_pub.strftime('%d/%m/%Y')
                    except:
                        data_publicacao_str = "Data não disponível"
                
                # Formatar timestamp de detecção
                timestamp_detectado = datetime.fromisoformat(resultado['timestamp']).strftime('%d/%m/%Y às %H:%M')
                
                # Incluir seção no nome da fonte
                fonte_com_secao = f"{resultado['fonte_nome']} - {resultado['secao']}"
                
                mensagem = (
                    f"🚨 *Palavras encontradas:* {palavras_str}\n"
                    f"📅 *Publicado:* {data_publicacao_str}\n"
                    f"🗞️ *{fonte_com_secao}*\n\n"
                    f"📰 {resultado['titulo']}\n\n"
                    f"🔗 {resultado['url']}\n\n"
                    f"⏰ *Detectado em:* {timestamp_detectado}"
                )
                
                await self.bot.send_message(
                    chat_id=chat_id,
                    text=mensagem,
                    parse_mode="Markdown",
                    disable_web_page_preview=True
                )
                
                # Pequena pausa entre mensagens para evitar rate limiting
                await asyncio.sleep(1)
                
        except Exception as e:
            logger.error(f"Erro ao enviar notificações: {e}")
    
    async def loop_monitoramento(self):
        """Loop principal do monitoramento."""
        logger.info("🚀 Loop de monitoramento iniciado")
        while self.running:
            try:
                await self.executar_monitoramento()
                await asyncio.sleep(MONITORAMENTO_INTERVAL)
            except Exception as e:
                logger.error(f"Erro no loop de monitoramento: {e}")
                await asyncio.sleep(60)  # Pausa menor em caso de erro
    
    def iniciar_monitoramento(self):
        """Inicia o monitoramento em background."""
        if not self.running:
            self.running = True
            logger.info("🚀 Monitoramento automático iniciado")
    
    def parar_monitoramento(self):
        """Para o monitoramento."""
        if self.running:
            self.running = False
            if self.monitoramento_task:
                self.monitoramento_task.cancel()
            logger.info("⏹️ Monitoramento automático parado")

# Instância global do gerenciador de monitoramento
monitor_manager = None

# Comando de ajuda
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Exibe o menu de ajuda com todos os comandos disponíveis."""
    help_text = (
        "🤖 *Faro Fino – Comandos Disponíveis:*\n\n"
        "🔹 Adicionar: `@termo1, termo2`\n"
        "🔹 Remover: `#termo1, termo2`\n\n"
        "📋 /verpalavras – Ver palavras-chave\n"
        "📋 /verperfis – Ver perfis/fontes\n"
        "🚀 /start – Configurar bot e boas-vindas\n"
        "🔄 /monitoramento – Ativar/desativar monitoramento\n"
        "⚡ /verificar – Executar monitoramento imediatamente\n"
        "📊 /status – Ver status do monitoramento\n"
        "🧹 /limparhistorico – Limpar histórico de links\n"
        "❓ /help – Mostrar este menu"
    )
    await update.message.reply_text(help_text, parse_mode="Markdown")

# Comando /start (substitui /config)
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando de início e configuração do bot."""
    user_id = update.message.from_user.id
    user_name = update.message.from_user.first_name or "usuário"
    
    if config_data["telegram_owner_id"] is None:
        config_data["telegram_owner_id"] = user_id
        salvar_config(config_data)
        mensagem = (
            f"🎉 *Bem-vindo ao Faro Fino, {user_name}!*\n\n"
            f"✅ Proprietário configurado! Seu ID: `{user_id}`\n\n"
            f"🔍 *O que é o Faro Fino?*\n"
            f"Sou um bot de monitoramento de notícias que te ajuda a ficar por dentro das últimas informações sobre os temas que você mais se interessa.\n\n"
            f"📝 *Como usar:*\n"
            f"• Adicione palavras-chave: `@política, economia`\n"
            f"• Adicione sites: `@https://g1.globo.com`\n"
            f"• Ative o monitoramento: `/monitoramento`\n"
            f"• Teste imediatamente: `/verificar`\n\n"
            f"🚀 *Pronto!* Agora você pode usar todos os comandos do bot.\n"
            f"Digite `/help` para ver todos os comandos disponíveis."
        )
    elif config_data["telegram_owner_id"] == user_id:
        mensagem = (
            f"👋 *Olá novamente, {user_name}!*\n\n"
            f"ℹ️ Você já é o proprietário configurado. ID: `{user_id}`\n\n"
            f"🔍 *Status atual:*\n"
            f"📌 Palavras-chave: {len(config_data.get('palavras_chave', []))}\n"
            f"📡 Fontes: {len(config_data.get('sites_monitorados', []) + config_data.get('perfis_twitter', []) + config_data.get('perfis_instagram', []))}\n"
            f"🔄 Monitoramento: {'🟢 Ativo' if config_data.get('monitoramento_ativo', False) else '🔴 Inativo'}\n\n"
            f"Digite `/help` para ver todos os comandos disponíveis."
        )
    else:
        mensagem = (
            f"👋 *Olá, {user_name}!*\n\n"
            f"❌ Este bot já possui um proprietário configurado.\n"
            f"Apenas o proprietário pode usar os comandos do Faro Fino.\n\n"
            f"🔒 Acesso restrito por questões de segurança."
        )
    
    await update.message.reply_text(mensagem, parse_mode="Markdown")

# Comando para verificação imediata
async def verificar_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Executa monitoramento imediatamente."""
    global monitor_manager
    
    if not verificar_proprietario(update):
        await update.message.reply_text("❌ Acesso negado.")
        return
    
    config = carregar_config()
    palavras = config.get("palavras_chave", [])
    fontes = config.get("sites_monitorados", [])
    
    if not palavras:
        await update.message.reply_text(
            "❌ *Erro:* Nenhuma palavra-chave configurada.\n"
            "Adicione palavras-chave primeiro usando: `@palavra1, palavra2`",
            parse_mode="Markdown"
        )
        return
    
    if not fontes:
        await update.message.reply_text(
            "❌ *Erro:* Nenhuma fonte configurada.\n"
            "Adicione sites primeiro usando: `@https://site.com`",
            parse_mode="Markdown"
        )
        return
    
    # Enviar mensagem de início
    await update.message.reply_text(
        f"⚡ *Executando verificação imediata...*\n\n"
        f"📌 Palavras-chave: {len(palavras)}\n"
        f"📡 Fontes: {len(fontes)}\n"
        f"🔍 Aguarde o resultado...",
        parse_mode="Markdown"
    )
    
    try:
        # Executar monitoramento imediatamente
        if monitor_manager:
            resultados = await monitor_manager.executar_monitoramento(executar_imediatamente=True)
            
            if resultados:
                await update.message.reply_text(
                    f"✅ *Verificação concluída!*\n\n"
                    f"📢 {len(resultados)} alerta(s) encontrado(s) e enviado(s).",
                    parse_mode="Markdown"
                )
            else:
                await update.message.reply_text(
                    f"✅ *Verificação concluída!*\n\n"
                    f"ℹ️ Nenhuma palavra-chave encontrada nas notícias recentes.",
                    parse_mode="Markdown"
                )
        else:
            await update.message.reply_text(
                "❌ *Erro:* Sistema de monitoramento não inicializado.",
                parse_mode="Markdown"
            )
            
    except Exception as e:
        logger.error(f"Erro na verificação imediata: {e}")
        await update.message.reply_text(
            f"❌ *Erro durante a verificação:* {str(e)}",
            parse_mode="Markdown"
        )

# Comando para limpar histórico
async def limpar_historico_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Limpa o histórico de links notificados."""
    if not verificar_proprietario(update):
        await update.message.reply_text("❌ Acesso negado.")
        return
    
    config = carregar_config()
    total_links = len(config.get("historico_links", {}))
    
    config["historico_links"] = {}
    salvar_config(config)
    
    await update.message.reply_text(
        f"🧹 *Histórico limpo com sucesso!*\n\n"
        f"📊 {total_links} links removidos do histórico.\n"
        f"O bot poderá notificar novamente sobre notícias antigas.",
        parse_mode="Markdown"
    )

# Comando para controlar o monitoramento
async def monitoramento_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ativa ou desativa o monitoramento automático."""
    global monitor_manager
    
    if not verificar_proprietario(update):
        await update.message.reply_text("❌ Acesso negado.")
        return
    
    config = carregar_config()
    monitoramento_ativo = config.get("monitoramento_ativo", False)
    
    if monitoramento_ativo:
        config["monitoramento_ativo"] = False
        salvar_config(config)
        if monitor_manager:
            monitor_manager.parar_monitoramento()
        await update.message.reply_text("⏹️ *Monitoramento desativado*", parse_mode="Markdown")
    else:
        # Verificar se há palavras-chave e fontes configuradas
        palavras = config.get("palavras_chave", [])
        fontes = config.get("sites_monitorados", [])
        
        if not palavras:
            await update.message.reply_text(
                "❌ *Erro:* Nenhuma palavra-chave configurada.\n"
                "Adicione palavras-chave primeiro usando: `@palavra1, palavra2`",
                parse_mode="Markdown"
            )
            return
        
        if not fontes:
            await update.message.reply_text(
                "❌ *Erro:* Nenhuma fonte configurada.\n"
                "Adicione sites primeiro usando: `@https://site.com`",
                parse_mode="Markdown"
            )
            return
        
        config["monitoramento_ativo"] = True
        salvar_config(config)
        
        # Iniciar o monitoramento
        if monitor_manager:
            monitor_manager.iniciar_monitoramento()
            # Criar task em background usando o contexto atual
            context.application.create_task(monitor_manager.loop_monitoramento())
        
        await update.message.reply_text(
            f"🚀 *Monitoramento ativado!*\n\n"
            f"📌 *Palavras-chave:* {len(palavras)}\n"
            f"📡 *Fontes:* {len(fontes)}\n"
            f"⏱️ *Intervalo:* {MONITORAMENTO_INTERVAL//60} minutos\n"
            f"🔍 *Filtro:* Notícias dos últimos {DIAS_FILTRO_NOTICIAS} dias\n"
            f"📰 *Monitoramento:* Links específicos de notícias",
            parse_mode="Markdown"
        )

# Comando para ver status do monitoramento
async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Exibe o status atual do monitoramento."""
    if not verificar_proprietario(update):
        await update.message.reply_text("❌ Acesso negado.")
        return
    
    config = carregar_config()
    monitoramento_ativo = config.get("monitoramento_ativo", False)
    ultima_verificacao = config.get("ultima_verificacao")
    
    palavras = config.get("palavras_chave", [])
    fontes = config.get("sites_monitorados", [])
    
    historico_links = len(config.get("historico_links", {}))
    
    status_emoji = "🟢" if monitoramento_ativo else "🔴"
    status_texto = "Ativo" if monitoramento_ativo else "Inativo"
    
    ultima_str = "Nunca"
    if ultima_verificacao:
        try:
            ultima_dt = datetime.fromisoformat(ultima_verificacao)
            ultima_str = ultima_dt.strftime('%d/%m/%Y %H:%M:%S')
        except:
            ultima_str = "Erro na data"
    
    mensagem = (
        f"📊 *Status do Monitoramento*\n\n"
        f"{status_emoji} *Status:* {status_texto}\n"
        f"📌 *Palavras-chave:* {len(palavras)}\n"
        f"📡 *Fontes:* {len(fontes)}\n"
        f"⏱️ *Intervalo:* {MONITORAMENTO_INTERVAL//60} minutos\n"
        f"🔍 *Filtro:* Últimos {DIAS_FILTRO_NOTICIAS} dias\n"
        f"📰 *Modo:* Links específicos de notícias\n"
        f"📚 *Histórico:* {historico_links} links\n"
        f"🕐 *Última verificação:* {ultima_str}"
    )
    
    await update.message.reply_text(mensagem, parse_mode="Markdown")

# Exibir palavras-chave
async def ver_palavras(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Exibe todas as palavras-chave cadastradas."""
    if not verificar_proprietario(update):
        await update.message.reply_text("❌ Acesso negado.")
        return
    
    palavras = config_data.get("palavras_chave", [])
    if palavras:
        texto = "📌 *Palavras-chave cadastradas:*\n\n" + "\n".join([f"• {palavra}" for palavra in palavras])
    else:
        texto = "📌 *Palavras-chave:*\n\nNenhuma palavra-chave cadastrada."
    
    await update.message.reply_text(texto, parse_mode="Markdown")

# Exibir fontes/perfis
async def ver_perfis(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Exibe todas as fontes e perfis cadastrados."""
    if not verificar_proprietario(update):
        await update.message.reply_text("❌ Acesso negado.")
        return
    
    sites = config_data.get("sites_monitorados", [])
    twitter = config_data.get("perfis_twitter", [])
    instagram = config_data.get("perfis_instagram", [])
    
    texto = "📡 *Fontes cadastradas:*\n\n"
    
    if sites:
        texto += "🌐 *Sites:*\n" + "\n".join([f"• {site}" for site in sites]) + "\n\n"
    
    if twitter:
        texto += "🐦 *Twitter/X:*\n" + "\n".join([f"• {perfil}" for perfil in twitter]) + "\n\n"
    
    if instagram:
        texto += "📸 *Instagram:*\n" + "\n".join([f"• {perfil}" for perfil in instagram]) + "\n\n"
    
    if not (sites or twitter or instagram):
        texto += "Nenhuma fonte cadastrada."
    
    await update.message.reply_text(texto, parse_mode="Markdown")

def verificar_proprietario(update: Update) -> bool:
    """Verifica se o usuário é o proprietário do bot."""
    user_id = update.message.from_user.id
    return config_data.get("telegram_owner_id") == user_id

# Adicionar ou remover termos via mensagens
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Processa mensagens para adicionar ou remover termos."""
    if not verificar_proprietario(update):
        await update.message.reply_text("❌ Acesso negado.")
        return

    text = update.message.text.strip()
    
    if not text.startswith(("@", "#")):
        await update.message.reply_text(
            "ℹ️ Use @ para adicionar ou # para remover termos.\n"
            "Exemplo: @termo1, termo2"
        )
        return
    
    termos = [t.strip() for t in text[1:].split(",") if t.strip()]
    
    # Remover @ do início de cada termo, caso o usuário tenha colocado @ em cada URL
    termos = [termo.lstrip('@') for termo in termos]
    
    if not termos:
        await update.message.reply_text("❌ Nenhum termo válido encontrado.")
        return

    if text.startswith("@"):
        # Adicionar termos
        for termo in termos:
            if termo.startswith("http"):
                # É uma URL
                if "twitter.com" in termo or "x.com" in termo:
                    if termo not in config_data["perfis_twitter"]:
                        config_data["perfis_twitter"].append(termo)
                elif "instagram.com" in termo:
                    if termo not in config_data["perfis_instagram"]:
                        config_data["perfis_instagram"].append(termo)
                else:
                    # Site comum
                    if termo not in config_data["sites_monitorados"]:
                        config_data["sites_monitorados"].append(termo)
            else:
                # É uma palavra-chave
                if termo not in config_data["palavras_chave"]:
                    config_data["palavras_chave"].append(termo)
        
        salvar_config(config_data)
        await update.message.reply_text(f"✅ {len(termos)} termo(s) adicionado(s) com sucesso!")

    elif text.startswith("#"):
        # Remover termos
        removidos = 0
        for termo in termos:
            if termo in config_data["palavras_chave"]:
                config_data["palavras_chave"].remove(termo)
                removidos += 1
            elif termo in config_data["sites_monitorados"]:
                config_data["sites_monitorados"].remove(termo)
                removidos += 1
            elif termo in config_data["perfis_twitter"]:
                config_data["perfis_twitter"].remove(termo)
                removidos += 1
            elif termo in config_data["perfis_instagram"]:
                config_data["perfis_instagram"].remove(termo)
                removidos += 1
        
        if removidos > 0:
            salvar_config(config_data)
            await update.message.reply_text(f"✅ {removidos} termo(s) removido(s) com sucesso!")
        else:
            await update.message.reply_text("❌ Nenhum dos termos especificados foi encontrado.")

def main():
    """Função principal do bot."""
    global monitor_manager
    
    # Verificar se o token do bot está configurado
    bot_token = os.getenv("BOT_TOKEN")
    if not bot_token:
        logger.error("❌ Variável de ambiente BOT_TOKEN não definida!")
        raise ValueError("❌ Variável de ambiente BOT_TOKEN não definida!")
    
    # Inicializar o gerenciador de monitoramento
    monitor_manager = MonitoramentoManager(bot_token)
    
    # Criar aplicação do bot
    application = ApplicationBuilder().token(bot_token).build()
    
    # Registrar handlers
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("verificar", verificar_command))
    application.add_handler(CommandHandler("monitoramento", monitoramento_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("verpalavras", ver_palavras))
    application.add_handler(CommandHandler("verperfis", ver_perfis))
    application.add_handler(CommandHandler("limparhistorico", limpar_historico_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    logger.info("✅ Faro Fino v2.7 iniciado com sucesso.")
    
    # Verificar se o monitoramento deve ser iniciado automaticamente
    config = carregar_config()
    if config.get("monitoramento_ativo", False):
        logger.info("🔄 Monitoramento estava ativo, reiniciando...")
        monitor_manager.iniciar_monitoramento()
        # Criar task em background usando o contexto da aplicação
        application.create_task(monitor_manager.loop_monitoramento())
    
    # Iniciar o bot usando run_polling sem asyncio.run
    application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()

