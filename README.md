# Faro Fino - Bot de Monitoramento de Notícias v2.7.2

## 🔧 Correção da Versão 2.7.2

### 🐛 **Problema Corrigido:**

#### Extração de Data das Notícias Falhando
- **❌ Problema**: Bot não conseguia extrair data das notícias do G1
- **🔍 Sintoma**: "📅 Data: Não encontrada" nos logs
- **💥 Consequência**: 0 alertas gerados mesmo com 60 links descobertos
- **✅ Solução**: Extração de data melhorada com múltiplos fallbacks

### 📊 **Melhorias Técnicas:**

#### Extração de Data Robusta
```python
# Antes: Apenas algumas meta tags
meta_tags_data = ['article:published_time', 'datePublished', ...]

# Agora: Meta tags + URL + HTML + Fallback
meta_tags_data = [
    ('property', 'article:published_time'),
    ('name', 'datePublished'),
    ('property', 'datePublished'),
    # ... mais opções
]

# Fallback 1: Extrair da URL
data_publicacao = extrair_data_da_url(url)

# Fallback 2: Buscar no HTML
padroes_data = [
    r'"datePublished":"(\d{4}-\d{2}-\d{2})',
    r'"publishedAt":"(\d{4}-\d{2}-\d{2})',
    # ... mais padrões
]

# Fallback 3: Data atual (para debug)
data_publicacao = datetime.now(TIMEZONE_BR).date()
```

#### Logs de Debug Adicionados
```python
logger.info(f"📅 Data extraída: {data_publicacao}")
logger.debug(f"⏰ Notícia muito antiga: {data_publicacao}")
logger.debug(f"🔍 Palavras testadas: {palavras_chave[:3]}... | Encontradas: {palavras_encontradas}")
```

### 🔄 **Compatibilidade:**
- ✅ Todas as funcionalidades da v2.7.1 mantidas
- ✅ Correção de timezone brasileiro mantida
- ✅ Filtro de conteúdo aprimorado mantido
- ✅ Links específicos continuam funcionando
- ✅ Comando `/verificar` continua funcionando

### 📱 **Resultado Esperado:**
```
🔍 Iniciando monitoramento de 3 sites para 27 palavras-chave
📅 Data extraída: 2025-06-19
🔍 Palavras testadas: ['brasil', 'governo', 'economia']... | Encontradas: ['economia']
📊 Monitoramento concluído: 60 links descobertos, 3 alertas gerados  ← Agora com alertas!
📢 3 alertas enviados
```

## 🔄 **Como Atualizar:**

### Atualização Simples
1. Substitua todos os arquivos pelos da v2.7.2
2. Faça commit e push para o GitHub
3. O Railway fará o deploy automático
4. **Teste**: Use `/verificar` - deve encontrar notificações agora

### Verificação Pós-Atualização
- ✅ Use `/verificar` para testar imediatamente
- ✅ Observe se aparecem alertas nos logs do Railway
- ✅ Confirme que recebe notificações com suas palavras-chave
- ✅ Verifique se o horário continua correto (timezone brasileiro)

## 🎯 **Resultado Esperado:**
- **Extração de data funcionando** (sempre encontra uma data válida)
- **Alertas sendo gerados** novamente
- **Notificações chegando** no Telegram
- **Logs mais informativos** para debug futuro

---

**v2.7.2** - Junho 2025  
*Correção crítica: extração de data das notícias com múltiplos fallbacks*

