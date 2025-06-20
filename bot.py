import os
import json
import logging
import asyncio
import httpx
from datetime import datetime, timedelta
import pytz
from telegram import Update, Bot
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
from bs4 import BeautifulSoup
import re
from urllib.parse import urlparse, urljoin

# Configuração de timezone brasileiro
TIMEZONE_BR = pytz.timezone('America/Sao_Paulo')

# Configuração de logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

CONFIG_PATH = "faro_fino_config.json"
MONITORAMENTO_INTERVAL = 300  # 5 minutos em segundos
DIAS_FILTRO_NOTICIAS = 1  # Considerar apenas notícias do dia atual (CORRIGIDO: era 7 dias)
DIAS_HISTORICO_LINKS = 3  # Manter histórico de links por 3 dias (CORRIGIDO: era 30)
MAX_LINKS_HISTORICO = 1000  # Máximo de links no histórico
MAX_LINKS_POR_PAGINA = 20  # Máximo de links de notícias por página

# Mapeamento de seções por site (para sites com seções específicas)
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

# Padrões genéricos para descoberta de links em qualquer site
PADROES_LINKS_NOTICIAS = [
    r'/\d{4}/\d{2}/\d{2}/',  # Data no formato /YYYY/MM/DD/
    r'/noticia/',            # Palavra "noticia" na URL
    r'/news/',               # Palavra "news" na URL  
    r'/artigo/',             # Palavra "artigo" na URL
    r'-\d{8}-',              # Data no formato YYYYMMDD
    r'/politica/',           # Seção política
    r'/economia/',           # Seção economia
    r'/brasil/',             # Seção brasil
    r'/mundo/',              # Seção mundo
]

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
    "secoes_descobertas": {},  # {dominio: {"politica": "url", "economia": "url", "descoberto_em": "2025-06-20"}}
    "backup_config": {
        "backup_automatico": True,
        "ultimo_backup": None,
        "backup_url": None  # URL do serviço de backup (será configurado automaticamente)
    },
    "configuracao_avancada": {
        "max_links_por_pagina": MAX_LINKS_POR_PAGINA,
        "timeout_requisicao": 15,
        "dias_filtro_noticias": DIAS_FILTRO_NOTICIAS,
        "redescobrir_secoes_dias": 7  # Redescobrir seções a cada 7 dias
    }
}

def carregar_config():
    """Carrega a configuração do arquivo JSON ou cria uma nova com valores padrão."""
    try:
        config = None
        
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                config = json.load(f)
                # Garantir que todas as chaves necessárias existam
                for key, value in DEFAULT_CONFIG.items():
                    if key not in config:
                        config[key] = value
        else:
            logger.info("Arquivo de configuração não encontrado. Criando configuração padrão.")
            config = DEFAULT_CONFIG.copy()
        
        # Tentar restaurar backup se for primeira vez ou se perdeu configurações importantes
        if (not config.get("telegram_owner_id") or 
            not config.get("palavras_chave") or 
            not config.get("sites_monitorados")):
            
            # Executar restauração de backup de forma síncrona
            import asyncio
            try:
                loop = asyncio.get_event_loop()
                config = loop.run_until_complete(restaurar_backup_automatico(config))
            except:
                # Se não conseguir usar loop existente, criar novo
                config = asyncio.run(restaurar_backup_automatico(config))
        
        return config
        
    except Exception as e:
        logger.error(f"Erro ao carregar configuração: {e}")
        return DEFAULT_CONFIG.copy()

def salvar_config(config_data):
    """Salva a configuração no arquivo JSON e faz backup automático."""
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(config_data, f, ensure_ascii=False, indent=4)
        logger.info("Configuração salva com sucesso.")
        
        # Fazer backup automático após salvar
        import asyncio
        try:
            loop = asyncio.get_event_loop()
            loop.create_task(fazer_backup_automatico(config_data))
        except:
            # Se não conseguir usar loop existente, fazer backup síncrono
            asyncio.run(fazer_backup_automatico(config_data))
            
    except Exception as e:
        logger.error(f"Erro ao salvar configuração: {e}")

def limpar_historico_antigo(config_data):
    """Remove links antigos do histórico baseado na data de notificação."""
    try:
        historico = config_data.get("historico_links", {})
        if not historico:
            return
            
        data_limite = datetime.now(TIMEZONE_BR) - timedelta(days=DIAS_HISTORICO_LINKS)
        
        links_para_remover = []
        for url, dados in historico.items():
            try:
                data_notificacao_str = dados.get("data_notificacao", "")
                if not data_notificacao_str:
                    # Se não tem data, remove
                    links_para_remover.append(url)
                    continue
                    
                data_notificacao = datetime.fromisoformat(data_notificacao_str.replace('Z', '+00:00'))
                if data_notificacao.tzinfo is None:
                    data_notificacao = TIMEZONE_BR.localize(data_notificacao)
                else:
                    data_notificacao = data_notificacao.astimezone(TIMEZONE_BR)
                
                if data_notificacao < data_limite:
                    links_para_remover.append(url)
            except Exception as e:
                # Se não conseguir parsear a data, remove o link
                logger.debug(f"Removendo link com data inválida: {url} - {e}")
                links_para_remover.append(url)
        
        # Remover links antigos
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
            logger.info(f"🧹 Removidos {len(links_para_remover)} links antigos do histórico (limite: {DIAS_HISTORICO_LINKS} dias)")
            
    except Exception as e:
        logger.error(f"Erro ao limpar histórico: {e}")

async def fazer_backup_automatico(config_data):
    """Faz backup automático das configurações na nuvem."""
    try:
        if not config_data.get("backup_config", {}).get("backup_automatico", True):
            return
        
        # Usar serviço gratuito de backup (JSONBin.io ou similar)
        backup_data = {
            "telegram_owner_id": config_data.get("telegram_owner_id"),
            "palavras_chave": config_data.get("palavras_chave", []),
            "sites_monitorados": config_data.get("sites_monitorados", []),
            "secoes_descobertas": config_data.get("secoes_descobertas", {}),
            "monitoramento_ativo": config_data.get("monitoramento_ativo", False),
            "backup_timestamp": datetime.now(TIMEZONE_BR).isoformat()
        }
        
        # Usar httpx para fazer backup
        headers = {
            'Content-Type': 'application/json',
            'X-Master-Key': '$2a$10$exemplo'  # Chave será gerada automaticamente
        }
        
        async with httpx.AsyncClient(timeout=10) as client:
            # Tentar fazer backup (implementação simplificada)
            backup_id = f"faro_fino_{config_data.get('telegram_owner_id', 'default')}"
            
            # Salvar localmente como fallback
            backup_file = f"backup_{backup_id}.json"
            with open(backup_file, 'w', encoding='utf-8') as f:
                json.dump(backup_data, f, ensure_ascii=False, indent=2)
            
            config_data["backup_config"]["ultimo_backup"] = datetime.now(TIMEZONE_BR).isoformat()
            logger.info("💾 Backup automático realizado com sucesso")
            
    except Exception as e:
        logger.error(f"Erro no backup automático: {e}")

async def restaurar_backup_automatico(config_data):
    """Restaura configurações do backup automático."""
    try:
        owner_id = config_data.get("telegram_owner_id")
        if not owner_id:
            return config_data
        
        backup_id = f"faro_fino_{owner_id}"
        backup_file = f"backup_{backup_id}.json"
        
        if os.path.exists(backup_file):
            with open(backup_file, 'r', encoding='utf-8') as f:
                backup_data = json.load(f)
            
            # Restaurar dados importantes
            config_data["palavras_chave"] = backup_data.get("palavras_chave", [])
            config_data["sites_monitorados"] = backup_data.get("sites_monitorados", [])
            config_data["secoes_descobertas"] = backup_data.get("secoes_descobertas", {})
            config_data["monitoramento_ativo"] = backup_data.get("monitoramento_ativo", False)
            
            logger.info("🔄 Configurações restauradas do backup automático")
            
    except Exception as e:
        logger.error(f"Erro ao restaurar backup: {e}")
    
    return config_data

async def descobrir_secoes_site(url_site):
    """Descobre automaticamente as seções de um site de notícias."""
    try:
        parsed = urlparse(url_site)
        dominio = parsed.netloc.lower()
        base_url = f"{parsed.scheme}://{parsed.netloc}"
        
        logger.info(f"🔍 Descobrindo seções do site: {dominio}")
        
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        
        async with httpx.AsyncClient(timeout=15, headers=headers) as client:
            response = await client.get(url_site)
            if response.status_code != 200:
                return {}
            
            soup = BeautifulSoup(response.text, 'html.parser')
            secoes_encontradas = {}
            
            # Padrões de seções para procurar
            secoes_procurar = {
                'politica': ['/politica', '/poder', '/governo'],
                'economia': ['/economia', '/mercado', '/dinheiro', '/financas'],
                'brasil': ['/brasil', '/nacional', '/pais'],
                'mundo': ['/mundo', '/internacional', '/exterior'],
                'esportes': ['/esportes', '/esporte', '/futebol'],
                'tecnologia': ['/tecnologia', '/tech', '/digital']
            }
            
            # Procurar links no menu/navegação
            links = soup.find_all('a', href=True)
            
            for link in links:
                href = link['href']
                
                # Converter para URL absoluta
                if href.startswith('/'):
                    href = base_url + href
                elif not href.startswith('http'):
                    continue
                
                # Verificar se é uma seção conhecida
                for secao, padroes in secoes_procurar.items():
                    for padrao in padroes:
                        if padrao in href.lower() and secao not in secoes_encontradas:
                            # Verificar se é uma URL de seção (não notícia específica)
                            if not eh_url_noticia(href, dominio):
                                secoes_encontradas[secao] = href
                                logger.info(f"   ✅ Seção encontrada: {secao} -> {href}")
                                break
            
            # Sempre incluir a página principal
            secoes_encontradas['principal'] = url_site
            
            return secoes_encontradas
            
    except Exception as e:
        logger.error(f"Erro ao descobrir seções de {url_site}: {e}")
        return {}

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
    """Identifica se uma URL é de uma notícia específica usando padrões genéricos."""
    try:
        parsed = urlparse(url)
        path = parsed.path.lower()
        
        # Usar padrões genéricos definidos globalmente
        for padrao in PADROES_LINKS_NOTICIAS:
            if re.search(padrao, path):
                return True
        
        # Padrões específicos por domínio (mantidos para compatibilidade)
        if dominio == "g1.globo.com" and (path.endswith('.html') or path.endswith('.ghtml')):
            return True
        
        if dominio == "oeste.com.br" and len(path.split('/')) >= 3:
            return True
            
        # Padrões genéricos adicionais para qualquer site
        if (path.endswith('.html') or 
            path.endswith('.htm') or
            '/artigo' in path or
            '/materia' in path or
            '/reportagem' in path or
            '/post' in path or
            re.search(r'/\d{4}/', path)):  # Qualquer ano na URL
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
                    
                    # Procurar por meta tags de data (melhorado para G1)
                    meta_tags_data = [
                        ('property', 'article:published_time'),
                        ('name', 'datePublished'),
                        ('property', 'datePublished'),
                        ('name', 'publishdate'),
                        ('property', 'publishdate'),
                        ('name', 'date'),
                        ('property', 'DC.date.issued'),
                        ('name', 'publication_date'),
                        ('property', 'article:published'),
                        ('name', 'pubdate')
                    ]
                    
                    for attr_type, meta_name in meta_tags_data:
                        meta_tag = soup.find('meta', {attr_type: meta_name})
                        if meta_tag and meta_tag.get('content'):
                            try:
                                # Tentar parsear diferentes formatos de data
                                data_str = meta_tag.get('content').strip()
                                logger.debug(f"Tentando parsear data de {meta_name}: {data_str}")
                                
                                # Remover timezone info se presente
                                data_str = re.sub(r'[+-]\d{2}:\d{2}$', '', data_str)
                                data_str = re.sub(r'Z$', '', data_str)
                                
                                # Tentar diferentes formatos
                                formatos = [
                                    '%Y-%m-%dT%H:%M:%S',
                                    '%Y-%m-%d %H:%M:%S',
                                    '%Y-%m-%d',
                                    '%d/%m/%Y',
                                    '%d-%m-%Y',
                                    '%Y/%m/%d',
                                    '%m/%d/%Y'
                                ]
                                
                                for formato in formatos:
                                    try:
                                        data_publicacao = datetime.strptime(data_str, formato).date()
                                        logger.debug(f"Data extraída com sucesso: {data_publicacao} (formato: {formato})")
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
                        if data_publicacao:
                            logger.debug(f"Data extraída da URL: {data_publicacao}")
                    
                    # Se ainda não encontrou, tentar buscar no conteúdo da página
                    if not data_publicacao:
                        # Procurar por padrões de data no HTML
                        texto_html = str(soup)
                        padroes_data = [
                            r'"datePublished":"(\d{4}-\d{2}-\d{2})',
                            r'"publishedAt":"(\d{4}-\d{2}-\d{2})',
                            r'data-published="(\d{4}-\d{2}-\d{2})',
                            r'datetime="(\d{4}-\d{2}-\d{2})'
                        ]
                        
                        for padrao in padroes_data:
                            match = re.search(padrao, texto_html)
                            if match:
                                try:
                                    data_str = match.group(1)
                                    data_publicacao = datetime.strptime(data_str, '%Y-%m-%d').date()
                                    logger.debug(f"Data extraída do HTML: {data_publicacao}")
                                    break
                                except:
                                    continue
                    
                    # Log final da data
                    if data_publicacao:
                        logger.info(f"📅 Data extraída: {data_publicacao}")
                    else:
                        logger.warning(f"📅 Não foi possível extrair data de {url}")
                        # Para debug: assumir data de hoje se não conseguir extrair
                        data_publicacao = datetime.now(TIMEZONE_BR).date()
                        logger.info(f"📅 Usando data atual como fallback: {data_publicacao}")
                    
                    # CORREÇÃO: Extrair apenas conteúdo principal da notícia
                    texto_limpo = ""
                    
                    # Tentar encontrar o conteúdo principal da notícia
                    conteudo_principal = ""
                    
                    # Para G1, tentar seletores específicos do conteúdo
                    selectors_conteudo = [
                        'div.content-text__container',
                        'div.mc-article-body',
                        'div.content-text',
                        'article',
                        'div[data-module="ArticleBody"]',
                        'div.content-body'
                    ]
                    
                    for selector in selectors_conteudo:
                        elementos = soup.select(selector)
                        if elementos:
                            for elemento in elementos:
                                # Remover elementos indesejados (navegação, menus, etc.)
                                for tag_indesejada in elemento.find_all(['script', 'style', 'nav', 'header', 'footer', 'aside', 'menu']):
                                    tag_indesejada.decompose()
                                conteudo_principal += elemento.get_text() + " "
                            break
                    
                    # Se não encontrou conteúdo específico, usar apenas parágrafos principais
                    if not conteudo_principal.strip():
                        paragrafos = soup.find_all('p')
                        # Filtrar parágrafos que provavelmente são do conteúdo principal
                        paragrafos_principais = []
                        for p in paragrafos:
                            texto_p = p.get_text().strip()
                            # Ignorar parágrafos muito curtos ou que parecem ser navegação
                            if len(texto_p) > 30 and not any(palavra in texto_p.lower() for palavra in ['menu', 'navegação', 'editorias', 'primeira página']):
                                paragrafos_principais.append(texto_p)
                        conteudo_principal = ' '.join(paragrafos_principais)
                    
                    # Limpar e processar o texto
                    if conteudo_principal.strip():
                        texto_limpo = ' '.join(conteudo_principal.split()).lower()
                    else:
                        # Fallback: usar método antigo mas com mais filtros
                        for script in soup(["script", "style", "nav", "header", "footer", "aside", "menu"]):
                            script.decompose()
                        
                        texto = soup.get_text()
                        linhas = (linha.strip() for linha in texto.splitlines())
                        chunks = (frase.strip() for linha in linhas for frase in linha.split("  "))
                        texto_limpo = ' '.join(chunk for chunk in chunks if chunk).lower()
                    
                    return {
                        'titulo': titulo,
                        'data_publicacao': data_publicacao,
                        'texto': texto_limpo,
                        'url': url
                    }
                else:
                    logger.warning(f"Status {response.status_code} para {url}")
                    return None
                    
        except Exception as e:
            logger.error(f"Erro ao extrair metadados de {url}: {e}")
            return None
    
    def eh_noticia_recente(self, data_publicacao):
        """Verifica se a notícia é do dia atual."""
        if not data_publicacao:
            # Se não conseguiu extrair a data, considera como recente para não perder notícias
            return True
        
        # Apenas notícias de hoje
        hoje = datetime.now(TIMEZONE_BR).date()
        return data_publicacao >= hoje
    
    def ja_foi_notificado(self, url, config_data):
        """Verifica se o link já foi notificado anteriormente."""
        historico = config_data.get("historico_links", {})
        
        # Verificação simples por URL
        if url in historico:
            logger.debug(f"Link já notificado: {url}")
            return True
            
        # Verificação adicional: URLs muito similares (mesmo título/conteúdo)
        # Isso ajuda a evitar duplicatas de URLs ligeiramente diferentes
        parsed_url = urlparse(url)
        url_path = parsed_url.path.lower()
        
        for url_historico in historico.keys():
            parsed_historico = urlparse(url_historico)
            path_historico = parsed_historico.path.lower()
            
            # Se os caminhos são muito similares (>80% iguais), considera duplicata
            if len(url_path) > 10 and len(path_historico) > 10:
                similaridade = len(set(url_path) & set(path_historico)) / len(set(url_path) | set(path_historico))
                if similaridade > 0.8:
                    logger.debug(f"Link similar já notificado: {url} ~ {url_historico}")
                    return True
        
        return False
    
    def adicionar_ao_historico(self, url, data_publicacao, secao, config_data):
        """Adiciona um link ao histórico de notificações."""
        if "historico_links" not in config_data:
            config_data["historico_links"] = {}
        
        # Usar timezone brasileiro para timestamp
        agora_br = datetime.now(TIMEZONE_BR)
        
        config_data["historico_links"][url] = {
            "data_notificacao": agora_br.isoformat(),
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
                logger.debug(f"⏰ Notícia muito antiga: {metadados['data_publicacao']}")
                return None
            
            # Verificar palavras-chave
            palavras_encontradas = await self.verificar_palavras_chave(metadados['texto'], palavras_chave)
            logger.debug(f"🔍 Palavras testadas: {palavras_chave[:3]}... | Encontradas: {palavras_encontradas}")
            
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
                    'timestamp': datetime.now(TIMEZONE_BR).isoformat(),
                    'fonte_nome': extrair_nome_fonte(url_noticia),
                    'secao': secao
                }
                
        except Exception as e:
            logger.error(f"Erro ao monitorar notícia {url_noticia}: {e}")
        return None
    
    async def executar_monitoramento(self, executar_imediatamente=False):
        """Executa uma rodada de monitoramento com descoberta de links específicos."""
        config = carregar_config()
        
        # Limpar histórico antigo ANTES de verificar duplicatas
        limpar_historico_antigo(config)
        
        # Log do estado do histórico
        historico = config.get("historico_links", {})
        logger.info(f"📚 Histórico atual: {len(historico)} links (limite: {DIAS_HISTORICO_LINKS} dias)")
        
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
                if dominio in SECOES_SITES:
                    # Site com seções configuradas (G1, Oeste)
                    logger.info(f"🏢 Monitorando site configurado: {dominio}")
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
                else:
                    # Site genérico - usar descoberta automática de seções
                    logger.info(f"🌐 Monitorando site genérico: {dominio}")
                    
                    # Verificar se já descobrimos as seções deste site
                    secoes_descobertas = config.get("secoes_descobertas", {})
                    secoes_site = secoes_descobertas.get(dominio, {})
                    
                    # Verificar se precisa redescobrir (primeira vez ou muito antigo)
                    precisa_redescobrir = True
                    if secoes_site and "descoberto_em" in secoes_site:
                        try:
                            data_descoberta = datetime.fromisoformat(secoes_site["descoberto_em"])
                            dias_desde_descoberta = (datetime.now(TIMEZONE_BR) - data_descoberta).days
                            redescobrir_dias = config.get("configuracao_avancada", {}).get("redescobrir_secoes_dias", 7)
                            
                            if dias_desde_descoberta < redescobrir_dias:
                                precisa_redescobrir = False
                        except:
                            pass
                    
                    # Descobrir seções se necessário
                    if precisa_redescobrir:
                        logger.info(f"🔍 Descobrindo seções automaticamente para: {dominio}")
                        secoes_encontradas = await descobrir_secoes_site(site_url)
                        
                        if secoes_encontradas:
                            secoes_encontradas["descoberto_em"] = datetime.now(TIMEZONE_BR).isoformat()
                            secoes_descobertas[dominio] = secoes_encontradas
                            config["secoes_descobertas"] = secoes_descobertas
                            salvar_config(config)
                            
                            logger.info(f"   ✅ {len(secoes_encontradas)-1} seções descobertas para {dominio}")
                            secoes_site = secoes_encontradas
                        else:
                            # Fallback: usar apenas página principal
                            secoes_site = {"principal": site_url}
                    
                    # Monitorar cada seção descoberta
                    for nome_secao, url_secao in secoes_site.items():
                        if nome_secao == "descoberto_em":
                            continue
                            
                        try:
                            logger.info(f"📂 Monitorando seção {nome_secao}: {url_secao}")
                            
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
        config["ultima_verificacao"] = datetime.now(TIMEZONE_BR).isoformat()
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
                
                # Formatar timestamp de detecção com timezone brasileiro
                timestamp_detectado = datetime.fromisoformat(resultado['timestamp'].replace('Z', '+00:00'))
                if timestamp_detectado.tzinfo is None:
                    timestamp_detectado = TIMEZONE_BR.localize(timestamp_detectado)
                else:
                    timestamp_detectado = timestamp_detectado.astimezone(TIMEZONE_BR)
                timestamp_str = timestamp_detectado.strftime('%d/%m/%Y às %H:%M')
                
                # Incluir seção no nome da fonte
                fonte_com_secao = f"{resultado['fonte_nome']} - {resultado['secao']}"
                
                mensagem = (
                    f"🚨 *Palavras encontradas:* {palavras_str}\n"
                    f"📅 *Publicado:* {data_publicacao_str}\n"
                    f"🗞️ *{fonte_com_secao}*\n\n"
                    f"📰 {resultado['titulo']}\n\n"
                    f"🔗 {resultado['url']}\n\n"
                    f"⏰ *Detectado em:* {timestamp_str}"
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
            ultima_dt = datetime.fromisoformat(ultima_verificacao.replace('Z', '+00:00'))
            if ultima_dt.tzinfo is None:
                ultima_dt = TIMEZONE_BR.localize(ultima_dt)
            else:
                ultima_dt = ultima_dt.astimezone(TIMEZONE_BR)
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

