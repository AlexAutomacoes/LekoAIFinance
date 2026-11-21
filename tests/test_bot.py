"""
Bateria de testes do roteamento do bot — sem rede, sem banco, sem IA.

Tudo que fala com o mundo de fora (Supabase, Groq, Telegram) é trocado por
dublês, então dá para rodar a qualquer hora, quantas vezes quiser, sem gastar
cota da Groq nem mexer em dado de cliente:

    python -m unittest discover -s tests -v

Cobre os fluxos que JÁ existiam (para garantir que nada quebrou) e o
editar/excluir lançamento + manual em PDF.
"""
import io
import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

# Os módulos leem env no import; valores falsos bastam, nada sai para a rede.
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "TESTE")
os.environ.setdefault("SUPABASE_URL", "https://exemplo.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "teste")
os.environ.setdefault("GROQ_API_KEY", "teste")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools import message_handler as mh  # noqa: E402
from tools import payment_service as ps  # noqa: E402

TG = 111
AGORA = datetime.now(timezone.utc)


def usuario(**extra):
    """Linha de `users` de um usuário comum do Grátis que já escolheu o plano."""
    row = {"id": 7, "telegram_id": TG, "name": "Ana", "plan_type": "free",
           "subscription_expires_at": None, "plano_escolhido_em": AGORA.isoformat(),
           "chamado_etapa": None, "chamado_problema": None, "chamado_iniciado_em": None,
           "edicao_gasto_id": None, "edicao_iniciada_em": None,
           "categoria_etapa": None, "categoria_iniciada_em": None}
    row.update(extra)
    return row


GASTO = {"id": 42, "status": "Saída", "valor": -50.0, "categoria": "Alimentação",
         "descricao": "mercado", "data": "2026-10-28"}

REGISTRO = {"acao": "registrar", "mensagem": "Registrei sua transação ✅",
            "transacao": {"status": "Saída", "valor": -45.5, "categoria": "Alimentação",
                          "descricao": "mercado", "data": "2026-10-29"}}


# Botões que aparecem embaixo da confirmação do registro (id 99 = dublê).
BOTOES_99 = [[{"texto": "✏️ Editar", "dados": "g|edit|99"},
              {"texto": "🗑️ Excluir", "dados": "g|del|99"}]]

CATEGORIAS = [{"id": 1, "nome": "Alimentação", "padrao": True},
              {"id": 2, "nome": "Lazer", "padrao": True},
              {"id": 3, "nome": "Outros", "padrao": True},
              {"id": 4, "nome": "Pets", "padrao": False}]


class BaseBot(unittest.TestCase):
    """Troca toda a I/O do message_handler por dublês."""

    def setUp(self):
        self.row = usuario()
        alvo = "tools.message_handler."
        self.p = {}
        for nome, valor in {
            "get_or_create_user_row": None,
            "extract_transaction": None,
            "insert_transaction": 99,  # id do lançamento criado
            "contar_lancamentos_mes": 0,
            "listar_ultimos_lancamentos": [GASTO],
            "get_lancamento": GASTO,
            "excluir_lancamento": True,
            "atualizar_lancamento": True,
            "salvar_edicao": None,
            "salvar_rascunho_chamado": None,
            "marcar_plano_escolhido": None,
            "get_transactions": [],
            "generate_financial_tips": "dica",
            "vincular_telegram": None,
            "listar_categorias": CATEGORIAS,
            "get_categoria": CATEGORIAS[0],
            "contar_categorias_proprias": 0,
            "contar_lancamentos_categoria": 4,
            "criar_categoria": {"id": 99, "nome": "Pets", "padrao": False},
            "renomear_categoria": ("renomeada", 4),
            "excluir_categoria": 4,
            "salvar_etapa_categoria": None,
        }.items():
            pat = mock.patch(alvo + nome, return_value=valor)
            self.p[nome] = pat.start()
            self.addCleanup(pat.stop)
        self.p["get_or_create_user_row"].side_effect = self._row
        env = mock.patch.dict(os.environ, {"PAYWALL_ENABLED": "true", "ADMIN_TELEGRAM_IDS": ""})
        env.start()
        self.addCleanup(env.stop)

    def _row(self, *a, retornar_criado=False, **k):
        return (self.row, False) if retornar_criado else self.row

    def msg(self, texto):
        return mh.process_message(texto, TG, "Ana")

    def ia(self, resposta):
        self.p["extract_transaction"].return_value = resposta


# ---------------------------------------------------------------------------
# Regressão: o que já existia continua igual
# ---------------------------------------------------------------------------
class TestRegressao(BaseBot):

    def test_menu_obrigatorio_bloqueia_quem_nao_escolheu(self):
        self.row = usuario(plano_escolhido_em=None)
        r = self.msg("gastei 50 no mercado")
        self.assertEqual(r[0]["tipo"], "botoes_planos")
        self.assertEqual(r[0]["gratis"], "escolher")
        self.p["extract_transaction"].assert_not_called()

    def test_menu_obrigatorio_tambem_vale_para_os_comandos_novos(self):
        self.row = usuario(plano_escolhido_em=None)
        for cmd in ("/lancamentos", "/manual"):
            self.assertEqual(self.msg(cmd)[0]["tipo"], "botoes_planos", cmd)

    def test_start_mostra_boas_vindas_com_comandos_novos(self):
        r = self.msg("/start")
        self.assertIn("meus lançamentos", r[0])
        self.assertIn('"manual"', r[0])
        self.assertIn("/chamado", r[0])
        self.assertIn("/assinaturas", r[0])
        # Só /assinaturas e /chamado aparecem com barra para o usuário.
        for cmd in ("/lancamentos", "/manual", "/categorias", "/plano", "/cancelar"):
            self.assertNotIn(cmd, r[0])

    def test_registrar_normal_insere_e_nao_edita(self):
        self.ia(REGISTRO)
        r = self.msg("gastei 45,50 no mercado")
        self.p["insert_transaction"].assert_called_once()
        self.p["atualizar_lancamento"].assert_not_called()
        self.assertEqual(r, [{"tipo": "botoes", "mensagem": "Registrei sua transação ✅", "botoes": BOTOES_99}])

    def test_cota_do_gratis_continua_bloqueando(self):
        self.ia(REGISTRO)
        self.p["contar_lancamentos_mes"].return_value = 20
        r = self.msg("gastei 10 no uber")
        self.assertEqual(r[0]["tipo"], "botoes_planos")
        self.assertEqual(r[0]["gratis"], "aguardar")
        self.p["insert_transaction"].assert_not_called()

    def test_relatorio_curto_no_chat(self):
        self.ia({"acao": "relatorio", "mensagem": "", "formato": "opcao",
                 "periodo": {"data_inicio": "2026-10-27", "data_fim": "2026-10-28"}})
        self.p["get_transactions"].return_value = [GASTO]
        r = self.msg("relatório de ontem")
        self.assertIn("Relatório Financeiro", r[0])

    def test_relatorio_longo_no_gratis_bloqueia(self):
        self.ia({"acao": "relatorio", "mensagem": "", "formato": "opcao",
                 "periodo": {"data_inicio": "2026-10-01", "data_fim": "2026-10-28"}})
        r = self.msg("relatório do mês")
        self.assertEqual(r[0]["tipo"], "botoes_planos")

    def test_conversar_pedir_dados_pedir_periodo(self):
        for acao in ("conversar", "pedir_dados", "pedir_periodo"):
            self.ia({"acao": acao, "mensagem": f"msg {acao}"})
            self.assertEqual(self.msg("oi"), [f"msg {acao}"])

    def test_chamado_continua_exclusivo_de_pago(self):
        self.assertEqual(self.msg("/chamado")[0]["tipo"], "botoes_planos")
        self.p["salvar_rascunho_chamado"].assert_not_called()

    def test_chamado_pago_e_rascunho(self):
        self.row = usuario(plan_type="lifetime")
        self.assertIn("1/2", self.msg("/chamado")[0])
        self.row = usuario(plan_type="lifetime", chamado_etapa="problema",
                           chamado_iniciado_em=AGORA.isoformat())
        r = self.msg("o relatório não abre no celular")
        self.assertIn("2/2", r[0])
        self.p["extract_transaction"].assert_not_called()

    def test_cancelar_sem_nada_em_andamento(self):
        self.assertIn("Não tem nada", self.msg("/cancelar")[0])

    def test_cancelar_rascunho_de_chamado(self):
        self.row = usuario(plan_type="lifetime", chamado_etapa="problema",
                           chamado_iniciado_em=AGORA.isoformat())
        self.assertIn("chamado cancelado", self.msg("/cancelar")[0])

    def test_assinaturas(self):
        self.assertEqual(self.msg("/assinaturas")[0]["tipo"], "botoes_planos")

    def test_acao_desconhecida_da_ia(self):
        self.ia({"acao": "inventada"})
        self.assertIn("desconhecida", self.msg("xyz")[0])

    def test_erro_interno_nao_explode(self):
        self.p["extract_transaction"].side_effect = RuntimeError("groq fora do ar")
        self.assertIn("erro interno", self.msg("gastei 5")[0])


# ---------------------------------------------------------------------------
# Novo: /lancamentos, ação "gerenciar" e os cliques g|...
# ---------------------------------------------------------------------------
class TestListaEClique(BaseBot):

    def test_lancamentos_sem_nada(self):
        self.p["listar_ultimos_lancamentos"].return_value = []
        r = self.msg("/lancamentos")
        self.assertIsInstance(r[0], str)
        self.assertIn("nenhum lançamento", r[0])

    def test_lancamentos_lista_em_botoes(self):
        r = self.msg("/lancamentos")
        self.assertEqual(r[0]["tipo"], "botoes")
        botao = r[0]["botoes"][0][0]
        self.assertEqual(botao["dados"], "g|ver|42")
        self.assertEqual(botao["texto"], "28/10 · Alimentação · -R$ 50,00")
        self.p["listar_ultimos_lancamentos"].assert_called_once_with(7, 10)

    def test_lancamentos_nao_passa_pela_ia_nem_pelo_paywall(self):
        self.row = usuario(plan_type="plus_monthly",
                           subscription_expires_at=(AGORA - timedelta(days=3)).isoformat())
        self.assertEqual(self.msg("/lancamentos")[0]["tipo"], "botoes")
        self.p["extract_transaction"].assert_not_called()

    def test_callback_data_cabe_no_limite_do_telegram(self):
        enorme = dict(GASTO, id=2**62, categoria="X" * 500)
        self.p["listar_ultimos_lancamentos"].return_value = [enorme]
        dados = self.msg("/lancamentos")[0]["botoes"][0][0]["dados"]
        self.assertLessEqual(len(dados.encode()), 64)
        self.p["get_lancamento"].return_value = enorme
        for acao in ("ver", "del"):
            r = mh.tratar_clique_lancamento(f"g|{acao}|{2**62}", TG, "Ana")
            for linha in r[0]["botoes"]:
                for b in linha:
                    self.assertLessEqual(len(b["dados"].encode()), 64)

    def test_ia_gerenciar_mostra_a_lista(self):
        self.ia({"acao": "gerenciar", "mensagem": "Escolha abaixo 👇"})
        r = self.msg("quero apagar o gasto do mercado")
        self.assertEqual(r[0]["tipo"], "botoes")
        self.assertEqual(r[0]["mensagem"], "Escolha abaixo 👇")
        self.p["excluir_lancamento"].assert_not_called()

    def test_ver(self):
        r = mh.tratar_clique_lancamento("g|ver|42", TG, "Ana")
        dados = [b["dados"] for linha in r[0]["botoes"] for b in linha]
        self.assertEqual(dados, ["g|edit|42", "g|del|42", "g|lista"])
        self.p["get_lancamento"].assert_called_once_with(7, 42)

    def test_lista_de_novo(self):
        self.assertEqual(mh.tratar_clique_lancamento("g|lista", TG, "Ana")[0]["tipo"], "botoes")

    def test_excluir_pede_confirmacao_antes(self):
        r = mh.tratar_clique_lancamento("g|del|42", TG, "Ana")
        self.assertIn("Tem certeza", r[0]["mensagem"])
        self.p["excluir_lancamento"].assert_not_called()

    def test_confirmar_exclusao(self):
        r = mh.tratar_clique_lancamento("g|delok|42", TG, "Ana")
        self.p["excluir_lancamento"].assert_called_once_with(7, 42)
        self.assertIn("excluído", r[0])

    def test_excluir_o_que_estava_sendo_editado_limpa_a_edicao(self):
        self.row = usuario(edicao_gasto_id=42, edicao_iniciada_em=AGORA.isoformat())
        mh.tratar_clique_lancamento("g|delok|42", TG, "Ana")
        self.p["salvar_edicao"].assert_called_once_with(7, None)

    def test_lancamento_de_outra_pessoa_nao_e_tocado(self):
        # get_lancamento filtra por dono: de outra pessoa, volta None
        self.p["get_lancamento"].return_value = None
        for acao in ("ver", "del", "delok", "edit"):
            r = mh.tratar_clique_lancamento(f"g|{acao}|999", TG, "Ana")
            self.assertEqual(r, [mh.MSG_LANCAMENTO_SUMIU], acao)
        self.p["excluir_lancamento"].assert_not_called()
        self.p["salvar_edicao"].assert_not_called()

    def test_cliques_malformados(self):
        for dados in ("g|", "g|ver", "g|ver|abc", "g|ver|", "g|xyz|42", "g|ver|1|2"):
            r = mh.tratar_clique_lancamento(dados, TG, "Ana")
            self.assertIsInstance(r, list, dados)
            self.assertTrue(r, dados)
        self.p["excluir_lancamento"].assert_not_called()

    def test_editar_comeca_a_edicao(self):
        r = mh.tratar_clique_lancamento("g|edit|42", TG, "Ana")
        self.p["salvar_edicao"].assert_called_once_with(7, 42)
        self.assertIn("O que você quer mudar", r[0])
        self.assertIn('"40 reais"', r[0])  # ensina a mandar só a correção
        self.assertIn('"cancelar"', r[0])

    def test_editar_derruba_rascunho_de_chamado(self):
        self.row = usuario(chamado_etapa="problema", chamado_iniciado_em=AGORA.isoformat())
        r = mh.tratar_clique_lancamento("g|edit|42", TG, "Ana")
        self.p["salvar_rascunho_chamado"].assert_called_once_with(7, None)
        self.assertIn("Cancelei o chamado", r[0])  # não some calado
        self.assertIn("O que você quer mudar", r[1])

    def test_editar_nao_avisa_de_chamado_ja_expirado(self):
        self.row = usuario(chamado_etapa="problema",
                           chamado_iniciado_em=(AGORA - timedelta(hours=2)).isoformat())
        r = mh.tratar_clique_lancamento("g|edit|42", TG, "Ana")
        self.assertEqual(len(r), 1)


# ---------------------------------------------------------------------------
# Novo: a mensagem depois do "Editar"
# ---------------------------------------------------------------------------
class TestEdicao(BaseBot):

    def setUp(self):
        super().setUp()
        self.row = usuario(edicao_gasto_id=42, edicao_iniciada_em=AGORA.isoformat())

    def test_mensagem_vira_a_correcao(self):
        self.ia(REGISTRO)
        r = self.msg("gastei 45,50 no mercado dia 29/10")
        self.p["atualizar_lancamento"].assert_called_once_with(
            user_id=7, gasto_id=42, status="Saída", valor=-45.5,
            categoria="Alimentação", descricao="mercado", data="2026-10-29")
        self.p["insert_transaction"].assert_not_called()
        self.p["contar_lancamentos_mes"].assert_not_called()  # corrigir não gasta cota
        self.p["salvar_edicao"].assert_called_once_with(7, None)
        texto = r[0]["mensagem"]
        self.assertIn("corrigido", texto)
        self.assertIn("Antes era", texto)
        self.assertIn("R$ 50,00", texto)   # o valor antigo aparece
        self.assertIn("R$ 45,50", texto)   # e o novo
        self.assertNotIn("Registrei", texto)
        self.assertEqual(r[0]["botoes"][0][0]["dados"], "g|edit|42")  # volta com os botões
        self.p["extract_transaction"].assert_called_once()

    def test_edicao_funciona_mesmo_com_cota_estourada(self):
        self.ia(REGISTRO)
        self.p["contar_lancamentos_mes"].return_value = 20
        self.msg("gastei 45,50 no mercado")
        self.p["atualizar_lancamento"].assert_called_once()

    def test_ia_pede_dados_e_a_edicao_continua_aberta(self):
        self.ia({"acao": "pedir_dados", "mensagem": "Qual o valor?"})
        r = self.msg("mercado")
        # Na edição a pergunta é sempre a nossa: a da IA costuma ser a padrão de
        # registro ("me conta tudo"), que é justamente o que não queremos aqui.
        self.assertEqual(r, [mh.MSG_PECA_DE_NOVO])
        self.p["atualizar_lancamento"].assert_not_called()
        self.p["salvar_edicao"].assert_not_called()

    def test_lancamento_sumiu_durante_a_edicao(self):
        self.ia(REGISTRO)
        self.p["atualizar_lancamento"].return_value = False
        r = self.msg("gastei 45 no mercado")
        self.assertEqual(r, [mh.MSG_LANCAMENTO_SUMIU])
        self.p["salvar_edicao"].assert_called_once_with(7, None)  # não fica preso

    def test_lancamento_apagado_antes_da_correcao(self):
        self.ia(REGISTRO)
        self.p["get_lancamento"].return_value = None
        self.assertEqual(self.msg("gastei 45 no mercado"), [mh.MSG_LANCAMENTO_SUMIU])
        self.p["atualizar_lancamento"].assert_not_called()
        self.p["salvar_edicao"].assert_called_once_with(7, None)

    def test_correcao_com_dados_ruins_nao_sobrescreve(self):
        ruins = [
            {"status": "Saída", "valor": "abc", "categoria": "X", "descricao": "", "data": "2026-10-29"},
            {"status": "Saída", "valor": 0, "categoria": "X", "descricao": "", "data": "2026-10-29"},
            {"status": "gasto", "valor": -5, "categoria": "X", "descricao": "", "data": "2026-10-29"},
            {"status": "Saída", "valor": -5, "categoria": "X", "descricao": "", "data": "2026-02-30"},
            {},
        ]
        for t in ruins:
            self.ia({"acao": "registrar", "mensagem": "Registrei", "transacao": t})
            self.assertEqual(self.msg("gastei algo"), [mh.MSG_PECA_DE_NOVO], t)
        self.ia({"acao": "registrar", "mensagem": "Registrei", "transacao": None})
        self.assertEqual(self.msg("gastei algo"), [mh.MSG_PECA_DE_NOVO])
        self.p["atualizar_lancamento"].assert_not_called()
        self.p["salvar_edicao"].assert_not_called()  # continua editando

    def test_correcao_acerta_sinal_e_categoria_vazia(self):
        self.ia({"acao": "registrar", "mensagem": "", "transacao": {
            "status": "Saída", "valor": 45.5, "categoria": None, "descricao": "x", "data": "2026-10-29"}})
        self.msg("gastei 45,50")
        kw = self.p["atualizar_lancamento"].call_args.kwargs
        self.assertEqual(kw["valor"], -45.5)
        self.assertEqual(kw["categoria"], "Alimentação")  # sem categoria = não mudou
        self.ia({"acao": "registrar", "mensagem": "", "transacao": {
            "status": "Entrada", "valor": -100, "categoria": "Salário", "descricao": "", "data": "2026-10-29"}})
        self.msg("recebi 100")
        self.assertEqual(self.p["atualizar_lancamento"].call_args.kwargs["valor"], 100.0)

    def test_pedir_relatorio_no_meio_da_edicao_sai_e_atende(self):
        self.ia({"acao": "relatorio", "mensagem": "", "formato": "opcao",
                 "periodo": {"data_inicio": "2026-10-27", "data_fim": "2026-10-28"}})
        self.p["get_transactions"].return_value = [GASTO]
        r = self.msg("relatório de ontem")
        self.assertEqual(r[0], mh.MSG_SAIU_DA_EDICAO)
        self.assertIn("Relatório Financeiro", r[1])
        self.p["salvar_edicao"].assert_called_once_with(7, None)
        self.p["atualizar_lancamento"].assert_not_called()
        self.p["extract_transaction"].assert_called_once()  # sem 2ª chamada à Groq

    def test_pedir_para_apagar_no_meio_da_edicao_mostra_a_lista(self):
        self.ia({"acao": "gerenciar", "mensagem": "Escolha 👇"})
        r = self.msg("quero apagar outro lançamento")
        self.assertEqual(r[0], mh.MSG_SAIU_DA_EDICAO)
        self.assertEqual(r[1]["tipo"], "botoes")

    def test_conversa_no_meio_da_edicao_sai_da_edicao(self):
        self.ia({"acao": "conversar", "mensagem": "De nada! 😉"})
        r = self.msg("obrigado")
        self.assertEqual(r, [mh.MSG_SAIU_DA_EDICAO, "De nada! 😉"])
        self.p["atualizar_lancamento"].assert_not_called()

    def test_cancelar(self):
        r = self.msg("/cancelar")
        self.assertIn("edição cancelada", r[0])
        self.p["salvar_edicao"].assert_called_once_with(7, None)
        self.p["atualizar_lancamento"].assert_not_called()

    def test_outro_comando_desiste_da_edicao_e_roda(self):
        r = self.msg("/assinaturas")
        self.p["salvar_edicao"].assert_called_once_with(7, None)
        self.assertEqual(r[0]["tipo"], "botoes_planos")

    def test_edicao_expirada_vira_registro_normal(self):
        self.row = usuario(edicao_gasto_id=42,
                           edicao_iniciada_em=(AGORA - timedelta(minutes=31)).isoformat())
        self.ia(REGISTRO)
        self.msg("gastei 45,50 no mercado")
        self.p["insert_transaction"].assert_called_once()
        self.p["atualizar_lancamento"].assert_not_called()

    def test_edicao_sem_data_confiavel_e_ignorada(self):
        self.row = usuario(edicao_gasto_id=42, edicao_iniciada_em=None)
        self.ia(REGISTRO)
        self.msg("gastei 45,50 no mercado")
        self.p["insert_transaction"].assert_called_once()

    def test_edicao_tem_prioridade_sobre_rascunho_velho_de_chamado(self):
        # rascunho de chamado expirado + edição ativa: a mensagem é a correção
        self.row = usuario(edicao_gasto_id=42, edicao_iniciada_em=AGORA.isoformat(),
                           chamado_etapa="problema",
                           chamado_iniciado_em=(AGORA - timedelta(hours=2)).isoformat())
        self.ia(REGISTRO)
        self.msg("gastei 45,50 no mercado")
        self.p["atualizar_lancamento"].assert_called_once()


# ---------------------------------------------------------------------------
# Novo: categorias personalizadas
# ---------------------------------------------------------------------------
class TestCategoriasNaIA(BaseBot):

    def test_ia_recebe_a_lista_do_usuario(self):
        self.ia(REGISTRO)
        self.msg("gastei 45,50 no mercado")
        args = self.p["extract_transaction"].call_args
        self.assertEqual(args.args[1], ["Alimentação", "Lazer", "Outros", "Pets"])

    def test_categoria_inventada_pela_ia_vira_outros(self):
        self.ia({**REGISTRO, "transacao": {**REGISTRO["transacao"], "categoria": "Supermercado"}})
        self.msg("gastei 45,50 no mercado")
        self.assertEqual(self.p["insert_transaction"].call_args.kwargs["categoria"], "Outros")

    def test_erro_de_digitacao_da_ia_acha_a_categoria_certa(self):
        # caso real visto no teste com a Groq: "Pets Test" para "Pets Teste"
        self.p["listar_categorias"].return_value = CATEGORIAS + [
            {"id": 5, "nome": "Pets Teste", "padrao": False}]
        self.ia({**REGISTRO, "transacao": {**REGISTRO["transacao"], "categoria": "Pets Test"}})
        r = self.msg("ração 80")
        self.assertEqual(self.p["insert_transaction"].call_args.kwargs["categoria"], "Pets Teste")
        # a confirmação mostra o que foi GRAVADO, não o que a IA escreveu
        self.assertIn("Categoria: Pets Teste", r[0]["mensagem"])

    def test_confirmacao_bate_com_o_gravado_quando_vai_para_outros(self):
        self.ia({**REGISTRO, "mensagem": "Registrei ✅ • Categoria: Supermercado",
                 "transacao": {**REGISTRO["transacao"], "categoria": "Supermercado"}})
        r = self.msg("gastei 45,50 no mercado")
        self.assertIn("Categoria: Outros", r[0]["mensagem"])
        self.assertNotIn("Supermercado", r[0]["mensagem"])
        self.assertEqual(r[0]["botoes"], BOTOES_99)

    def test_categoria_ja_certa_mantem_a_mensagem_da_ia(self):
        self.ia(REGISTRO)  # categoria "Alimentação", que está na lista
        self.assertEqual(self.msg("gastei 45,50 no mercado"), [{"tipo": "botoes", "mensagem": "Registrei sua transação ✅", "botoes": BOTOES_99}])

    def test_categoria_com_caixa_diferente_vira_a_da_lista(self):
        self.ia({**REGISTRO, "transacao": {**REGISTRO["transacao"], "categoria": "  pets "}})
        self.msg("ração 80")
        self.assertEqual(self.p["insert_transaction"].call_args.kwargs["categoria"], "Pets")

    def test_correcao_tambem_usa_a_lista(self):
        self.row = usuario(edicao_gasto_id=42, edicao_iniciada_em=AGORA.isoformat())
        self.ia({**REGISTRO, "transacao": {**REGISTRO["transacao"], "categoria": "Inventada"}})
        self.msg("gastei 45,50 no mercado")
        self.assertEqual(self.p["atualizar_lancamento"].call_args.kwargs["categoria"], "Outros")

    def test_falha_ao_ler_categorias_nao_derruba_o_registro(self):
        self.p["listar_categorias"].side_effect = RuntimeError("banco fora")
        self.ia(REGISTRO)
        r = self.msg("gastei 45,50 no mercado")
        self.assertEqual(r, [{"tipo": "botoes", "mensagem": "Registrei sua transação ✅", "botoes": BOTOES_99}])
        self.assertIsNone(self.p["extract_transaction"].call_args.args[1])


class TestTelaDeCategorias(BaseBot):

    def clique(self, dados):
        return mh.tratar_clique_categoria(dados, TG, "Ana")

    def test_comando_lista_em_botoes(self):
        r = self.msg("/categorias")
        self.assertEqual(r[0]["tipo"], "botoes")
        linhas = r[0]["botoes"]
        self.assertEqual(linhas[0], [{"texto": "Alimentação", "dados": "c|ver|1"},
                                     {"texto": "Lazer", "dados": "c|ver|2"}])
        self.assertEqual(linhas[-1], [{"texto": "➕ Nova categoria", "dados": "c|nova"}])
        self.p["extract_transaction"].assert_not_called()

    def test_callback_data_cabe_no_limite_do_telegram(self):
        self.p["listar_categorias"].return_value = [{"id": 2**62, "nome": "X" * 30, "padrao": False}]
        for linha in self.msg("/categorias")[0]["botoes"]:
            for b in linha:
                self.assertLessEqual(len(b["dados"].encode()), 64)

    def test_ver_categoria(self):
        r = self.clique("c|ver|1")
        self.assertIn("4 lançamento", r[0]["mensagem"])
        dados = [b["dados"] for l in r[0]["botoes"] for b in l]
        self.assertEqual(dados, ["c|ren|1", "c|del|1", "c|lista"])

    def test_outros_nao_pode_renomear_nem_excluir(self):
        self.p["get_categoria"].return_value = CATEGORIAS[2]
        for acao in ("ver", "ren", "del", "delok"):
            r = self.clique(f"c|{acao}|3")
            self.assertIn("reserva", r[0]["mensagem"], acao)
        self.p["excluir_categoria"].assert_not_called()
        self.p["salvar_etapa_categoria"].assert_not_called()

    def test_excluir_pede_confirmacao_e_avisa_destino(self):
        r = self.clique("c|del|1")
        self.assertIn('vão para "Outros"', r[0]["mensagem"])
        self.p["excluir_categoria"].assert_not_called()
        r = self.clique("c|delok|1")
        self.p["excluir_categoria"].assert_called_once_with(7, 1)
        self.assertIn("excluída", r[0]["mensagem"])

    def test_categoria_de_outra_pessoa(self):
        self.p["get_categoria"].return_value = None
        for acao in ("ver", "ren", "del", "delok"):
            self.assertIn("Não encontrei", self.clique(f"c|{acao}|999")[0], acao)
        self.p["excluir_categoria"].assert_not_called()
        self.p["salvar_etapa_categoria"].assert_not_called()

    def test_cliques_malformados(self):
        for dados in ("c|", "c|ver", "c|ver|abc", "c|xyz|1", "c|del|"):
            self.assertTrue(self.clique(dados), dados)
        self.p["excluir_categoria"].assert_not_called()

    def test_nova_no_gratis_dentro_do_limite(self):
        self.p["contar_categorias_proprias"].return_value = 2
        r = self.clique("c|nova")
        self.p["salvar_etapa_categoria"].assert_called_once_with(7, "nova")
        self.assertIn("nome da nova categoria", r[0])

    def test_nova_no_gratis_acima_do_limite_mostra_upgrade(self):
        self.p["contar_categorias_proprias"].return_value = 3
        r = self.clique("c|nova")
        self.assertEqual(r[0]["tipo"], "botoes_planos")
        self.assertIn("até 3 categorias", r[0]["mensagem"])
        self.p["salvar_etapa_categoria"].assert_not_called()

    def test_nova_no_plano_pago_sem_limite(self):
        self.row = usuario(plan_type="lifetime")
        self.p["contar_categorias_proprias"].return_value = 15
        self.clique("c|nova")
        self.p["salvar_etapa_categoria"].assert_called_once_with(7, "nova")

    def test_teto_total_vale_para_todos(self):
        self.row = usuario(plan_type="lifetime")
        self.p["listar_categorias"].return_value = [{"id": i, "nome": f"C{i}", "padrao": True}
                                                    for i in range(30)]
        r = self.clique("c|nova")
        self.assertIn("máximo", r[0])
        self.p["salvar_etapa_categoria"].assert_not_called()

    def test_renomear_nao_tem_limite_de_plano(self):
        self.p["contar_categorias_proprias"].return_value = 3
        self.clique("c|ren|1")
        self.p["salvar_etapa_categoria"].assert_called_once_with(7, "renomear:1")

    def test_abrir_conversa_desfaz_edicao_e_avisa_chamado(self):
        self.row = usuario(edicao_gasto_id=42, edicao_iniciada_em=AGORA.isoformat(),
                           chamado_etapa="problema", chamado_iniciado_em=AGORA.isoformat())
        r = self.clique("c|ren|1")
        self.p["salvar_edicao"].assert_called_once_with(7, None)
        self.p["salvar_rascunho_chamado"].assert_called_once_with(7, None)
        self.assertIn("Cancelei o chamado", r[0])

    def test_editar_lancamento_desfaz_nome_de_categoria_pendente(self):
        self.row = usuario(categoria_etapa="nova", categoria_iniciada_em=AGORA.isoformat())
        mh.tratar_clique_lancamento("g|edit|42", TG, "Ana")
        self.p["salvar_etapa_categoria"].assert_called_once_with(7, None)


class TestNomeDeCategoria(BaseBot):

    def etapa(self, etapa, minutos_atras=0):
        self.row = usuario(categoria_etapa=etapa,
                           categoria_iniciada_em=(AGORA - timedelta(minutes=minutos_atras)).isoformat())

    def test_criar(self):
        self.etapa("nova")
        r = self.msg("  pets  ")
        self.p["criar_categoria"].assert_called_once_with(7, "Pets")
        self.p["salvar_etapa_categoria"].assert_called_once_with(7, None)
        self.assertIn("criada", r[0]["mensagem"])
        self.p["extract_transaction"].assert_not_called()  # o nome não vai para a IA

    def test_criar_duplicada(self):
        self.etapa("nova")
        self.p["criar_categoria"].return_value = None
        self.assertIn("já tem", self.msg("Lazer")[0]["mensagem"])

    def test_limite_conferido_de_novo_ao_receber_o_nome(self):
        self.etapa("nova")
        self.p["contar_categorias_proprias"].return_value = 3
        r = self.msg("Academia")
        self.assertEqual(r[0]["tipo"], "botoes_planos")
        self.p["criar_categoria"].assert_not_called()

    def test_nome_invalido_pede_de_novo_e_segue_esperando(self):
        self.etapa("nova")
        for ruim in ("a", "12345", "x" * 31, "!!"):
            self.assertIn("não serve", self.msg(ruim)[0], ruim)
        self.p["criar_categoria"].assert_not_called()
        self.p["salvar_etapa_categoria"].assert_not_called()

    def test_renomear(self):
        self.etapa("renomear:1")
        r = self.msg("Comida")
        self.p["renomear_categoria"].assert_called_once_with(7, 1, "Comida")
        self.assertIn('agora se chama "Comida"', r[0]["mensagem"])
        self.assertIn("4 lançamento", r[0]["mensagem"])

    def test_renomear_juntando(self):
        self.etapa("renomear:1")
        self.p["renomear_categoria"].return_value = ("juntada", 2)
        self.assertIn("juntada", self.msg("Lazer")[0]["mensagem"])

    def test_cancelar(self):
        self.etapa("renomear:1")
        r = self.msg("/cancelar")
        self.assertIn("cancelei", r[0])
        self.p["renomear_categoria"].assert_not_called()

    def test_outro_comando_desiste_e_roda(self):
        self.etapa("nova")
        r = self.msg("/plano")
        self.p["salvar_etapa_categoria"].assert_called_once_with(7, None)
        self.p["criar_categoria"].assert_not_called()
        self.assertIn("Seu plano", r[0])

    def test_etapa_expirada_vira_mensagem_normal(self):
        self.etapa("nova", minutos_atras=31)
        self.ia(REGISTRO)
        self.msg("gastei 45,50 no mercado")
        self.p["criar_categoria"].assert_not_called()
        self.p["insert_transaction"].assert_called_once()

    def test_etapa_corrompida(self):
        self.etapa("renomear:abc")
        self.assertIn("Não consegui", self.msg("Comida")[0])

# ---------------------------------------------------------------------------
# Novo: manual em PDF
# ---------------------------------------------------------------------------
class TestManual(BaseBot):

    def test_arquivo_existe_e_e_pdf(self):
        self.assertTrue(os.path.isfile(mh.CAMINHO_MANUAL), mh.CAMINHO_MANUAL)
        with open(mh.CAMINHO_MANUAL, "rb") as f:
            self.assertEqual(f.read(5), b"%PDF-")
        # O Telegram aceita documento de até 50 MB por bot.
        self.assertLess(os.path.getsize(mh.CAMINHO_MANUAL), 50 * 1024 * 1024)

    def test_comando_manual(self):
        r = self.msg("/manual")
        self.assertEqual(r[0]["tipo"], "documento")
        self.assertEqual(r[0]["caminho"], mh.CAMINHO_MANUAL)

    def test_primeira_escolha_do_gratis_manda_o_manual(self):
        self.row = usuario(plano_escolhido_em=None)
        r = mh.escolher_plano_gratis(TG, "Ana")
        self.assertIsInstance(r[0], str)
        self.assertEqual(r[-1]["tipo"], "documento")
        self.p["marcar_plano_escolhido"].assert_called_once_with(7)

    def test_clicar_de_novo_no_gratis_nao_reenvia(self):
        r = mh.escolher_plano_gratis(TG, "Ana")
        self.assertFalse(any(isinstance(x, dict) for x in r))


# ---------------------------------------------------------------------------
# Webhook de pagamento: o contrato de `notificar`
# ---------------------------------------------------------------------------
class TestPagamentoManual(unittest.TestCase):

    def _ativar(self, renovacao, plano_atual="free"):
        tabelas = {
            "pagamentos": mock.MagicMock(),
            "users": mock.MagicMock(),
        }
        tabelas["pagamentos"].select.return_value.eq.return_value.execute.return_value.data = []
        tabelas["users"].select.return_value.eq.return_value.execute.return_value.data = [
            {"id": 7, "plan_type": plano_atual, "subscription_expires_at": None}]
        cliente = mock.MagicMock()
        cliente.table.side_effect = lambda nome: tabelas[nome]
        with mock.patch.object(ps, "_client", return_value=cliente), \
             mock.patch.object(ps, "aplicar_plano"):
            return ps._ativar(chave="in_1", external_id=str(TG), plan_type="plus_monthly",
                              email="a@b.c", evento={}, renovacao=renovacao)

    def test_primeira_ativacao_pede_manual(self):
        status, _, notificar = self._ativar(renovacao=False)
        self.assertEqual(status, 200)
        self.assertEqual(notificar["telegram_id"], TG)
        self.assertTrue(notificar["manual"])
        self.assertIn("Pagamento confirmado", notificar["texto"])

    def test_recompra_ou_upgrade_de_plano_pago_nao_pede_manual(self):
        for atual in ("plus_annual", "plus_monthly"):
            _, _, notificar = self._ativar(renovacao=False, plano_atual=atual)
            self.assertFalse(notificar["manual"], atual)

    def test_renovacao_nao_pede_manual(self):
        _, _, notificar = self._ativar(renovacao=True)
        self.assertFalse(notificar["manual"])
        self.assertIn("Renovação", notificar["texto"])


# ---------------------------------------------------------------------------
# api/telegram.py: o que vai para a API do Telegram
# ---------------------------------------------------------------------------
class TestTelegramApi(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from api import telegram as tg
        cls.tg = tg

    def setUp(self):
        self.enviados = []
        pat = mock.patch("urllib.request.urlopen", side_effect=self._capturar)
        pat.start()
        self.addCleanup(pat.stop)

    def _capturar(self, req, timeout=None):
        self.enviados.append((req.full_url, req.data))
        return mock.MagicMock()

    def test_tipo_botoes_vira_inline_keyboard(self):
        self.tg.enviar_respostas(TG, [{"tipo": "botoes", "mensagem": "Escolha",
                                       "botoes": [[{"texto": "A", "dados": "g|ver|1"},
                                                   {"texto": "B", "dados": "g|del|1"}],
                                                  [{"texto": "C", "dados": "g|lista"}]]}])
        url, corpo = self.enviados[0]
        self.assertTrue(url.endswith("/sendMessage"))
        payload = json.loads(corpo)
        self.assertEqual(payload["text"], "Escolha")
        self.assertEqual(payload["reply_markup"]["inline_keyboard"],
                         [[{"text": "A", "callback_data": "g|ver|1"},
                           {"text": "B", "callback_data": "g|del|1"}],
                          [{"text": "C", "callback_data": "g|lista"}]])

    def test_texto_e_tipos_antigos_continuam_funcionando(self):
        with mock.patch.object(self.tg, "logging") as log:
            self.tg.enviar_respostas(TG, [
                "oi",
                {"tipo": "botoes_planos", "mensagem": "planos", "gratis": "escolher"},
                {"tipo": "botoes_formato", "mensagem": "fmt", "data_inicio": "a", "data_fim": "b"},
                {"tipo": "botao_link", "mensagem": "pagar", "texto_botao": "Pagar", "url": "https://x"},
            ])
            log.warning.assert_not_called()
        self.assertEqual(len(self.enviados), 4)

    def _handler(self, corpo: dict, path="/api/telegram", headers=None):
        h = object.__new__(self.tg.handler)
        raw = json.dumps(corpo).encode()
        h.headers = {"Content-Length": str(len(raw)), **(headers or {})}
        h.rfile, h.wfile, h.path = io.BytesIO(raw), io.BytesIO(), path
        h.respostas = []
        h.send_response = h.respostas.append
        h.send_header = lambda *a: None
        h.end_headers = lambda: None
        return h

    def test_clique_g_vai_para_o_handler_de_lancamentos(self):
        upd = {"callback_query": {"id": "1", "data": "g|ver|42",
                                  "from": {"id": TG, "first_name": "Ana"},
                                  "message": {"chat": {"id": TG}}}}
        h = self._handler(upd)
        with mock.patch.object(self.tg, "WEBHOOK_SECRET", None), \
             mock.patch("tools.message_handler.tratar_clique_lancamento",
                        return_value=["ok"]) as clique:
            h.do_POST()
        clique.assert_called_once_with("g|ver|42", TG, "Ana")
        self.assertEqual(h.respostas, [200])

    def test_webhook_sem_secret_continua_401(self):
        h = self._handler({"message": {}})
        with mock.patch.object(self.tg, "WEBHOOK_SECRET", "segredo"):
            h.do_POST()
        self.assertEqual(h.respostas, [401])

    def test_pagamento_com_manual_envia_o_pdf(self):
        h = self._handler({}, path="/api/pagamento")
        notificar = {"telegram_id": TG, "texto": "✅ Pagamento confirmado!", "manual": True}
        with mock.patch.object(ps, "handle_webhook", return_value=(200, {}, notificar)), \
             mock.patch.object(self.tg, "send_document") as doc:
            h.do_POST()
        doc.assert_called_once()
        self.assertEqual(doc.call_args[0][1], mh.CAMINHO_MANUAL)

    def test_renovacao_nao_envia_o_pdf(self):
        h = self._handler({}, path="/api/pagamento")
        notificar = {"telegram_id": TG, "texto": "🔄 Renovação", "manual": False}
        with mock.patch.object(ps, "handle_webhook", return_value=(200, {}, notificar)), \
             mock.patch.object(self.tg, "send_document") as doc:
            h.do_POST()
        doc.assert_not_called()
        self.assertEqual(len(self.enviados), 1)  # só o texto


# ---------------------------------------------------------------------------
# Novo: botões no registro e comandos sem "/" (só /assinaturas e /chamado)
# ---------------------------------------------------------------------------
class TestBotoesERegistroSemBarra(BaseBot):

    def test_registro_vem_com_editar_e_excluir_do_lancamento_criado(self):
        self.ia(REGISTRO)
        r = self.msg("gastei 45,50 no mercado")
        self.assertEqual(r[0]["tipo"], "botoes")
        self.assertEqual(r[0]["botoes"], BOTOES_99)

    def test_botao_do_registro_cai_no_fluxo_da_lista(self):
        # O clique em Excluir embaixo do registro é o mesmo g|del da lista.
        r = mh.tratar_clique_lancamento("g|del|42", TG, "Ana")
        self.assertIn("Tem certeza", r[0]["mensagem"])

    def test_falha_ao_gravar_nao_manda_botao(self):
        self.ia(REGISTRO)
        self.p["insert_transaction"].return_value = None
        self.assertEqual(self.msg("gastei 45,50 no mercado"), ["Falha ao salvar no banco de dados."])

    def test_atalhos_em_texto_nao_chamam_a_ia(self):
        casos = {"Meus lançamentos": "botoes", "minhas categorias": "botoes",
                 "Manual": "documento", "meu plano.": None}
        for texto, tipo in casos.items():
            r = self.msg(texto)
            if tipo:
                self.assertEqual(r[0]["tipo"], tipo, texto)
            else:
                self.assertIn("Seu plano", r[0], texto)
        self.p["extract_transaction"].assert_not_called()

    def test_cancelar_em_texto_cancela_a_edicao(self):
        self.row = usuario(edicao_gasto_id=42, edicao_iniciada_em=AGORA.isoformat())
        r = self.msg("Cancelar")
        self.assertIn("edição cancelada", r[0])
        self.p["extract_transaction"].assert_not_called()
        self.p["atualizar_lancamento"].assert_not_called()

    def test_cancelar_em_texto_cancela_o_chamado(self):
        self.row = usuario(chamado_etapa="problema", chamado_iniciado_em=AGORA.isoformat())
        self.assertIn("chamado cancelado", self.msg("cancelar")[0])

    def test_frase_com_atalho_no_meio_vai_para_a_ia(self):
        self.ia({"acao": "conversar", "mensagem": "oi"})
        self.msg("quero cancelar minha assinatura")
        self.p["extract_transaction"].assert_called_once()

    def test_acao_abrir_da_ia(self):
        self.ia({"acao": "abrir", "tela": "categorias", "mensagem": "Claro!"})
        self.assertEqual(self.msg("quero ver as categorias que eu tenho")[0]["tipo"], "botoes")
        self.ia({"acao": "abrir", "tela": "manual", "mensagem": "Claro!"})
        self.assertEqual(self.msg("como eu uso esse bot?")[0]["tipo"], "documento")
        self.ia({"acao": "abrir", "tela": "plano", "mensagem": "Claro!"})
        self.assertIn("Seu plano", self.msg("qual é o meu plano atual?")[0])
        self.ia({"acao": "abrir", "tela": "inventada", "mensagem": "Hmm"})
        self.assertEqual(self.msg("abre a tela x"), ["Hmm"])

    def test_comandos_antigos_continuam_funcionando_escondidos(self):
        self.assertEqual(self.msg("/lancamentos")[0]["tipo"], "botoes")
        self.assertEqual(self.msg("/categorias")[0]["tipo"], "botoes")
        self.assertEqual(self.msg("/manual")[0]["tipo"], "documento")

    def test_respostas_do_bot_nao_citam_comandos_escondidos(self):
        # Varre os textos do código: nenhuma mensagem ao usuário pode mandar a
        # pessoa usar um comando que saiu do menu. Ficam de fora as docstrings
        # e os próprios nomes dos comandos (ATALHOS, startswith).
        import ast
        arvore = ast.parse(open(mh.__file__, encoding="utf-8").read())
        docstrings = {id(n.body[0].value) for n in ast.walk(arvore)
                      if isinstance(n, (ast.Module, ast.FunctionDef)) and n.body
                      and isinstance(n.body[0], ast.Expr)}
        for no in ast.walk(arvore):
            if (isinstance(no, ast.Constant) and isinstance(no.value, str)
                    and id(no) not in docstrings and not no.value.startswith("/")):
                for cmd in ("/lancamentos", "/categorias", "/manual", "/plano", "/cancelar"):
                    self.assertNotIn(cmd, no.value, f"linha {no.lineno}")

# ---------------------------------------------------------------------------
# Novo: edição parcial — a pessoa manda só o que muda ("40 reais")
# ---------------------------------------------------------------------------
class TestEdicaoParcial(BaseBot):

    def setUp(self):
        super().setUp()
        self.row = usuario(edicao_gasto_id=42, edicao_iniciada_em=AGORA.isoformat())

    def corrige(self, campos, texto="x"):
        self.ia({"acao": "corrigir", "campos": campos, "mensagem": ""})
        return self.msg(texto)

    def gravado(self):
        return self.p["atualizar_lancamento"].call_args.kwargs

    def test_so_o_valor_muda_o_resto_fica(self):
        r = self.corrige({"valor": 40}, "40 reais")
        self.assertEqual(self.gravado(), dict(
            user_id=7, gasto_id=42, status="Saída", valor=-40.0,
            categoria="Alimentação", descricao="mercado", data="2026-10-28"))
        self.assertIn("R$ 40,00", r[0]["mensagem"])

    def test_a_ia_recebe_o_lancamento_que_esta_sendo_editado(self):
        self.corrige({"valor": 40}, "40 reais")
        self.assertEqual(self.p["extract_transaction"].call_args.args[2], GASTO)

    def test_era_entrada_inverte_o_sinal(self):
        self.corrige({"status": "Entrada"}, "era entrada")
        self.assertEqual(self.gravado()["valor"], 50.0)
        self.assertEqual(self.gravado()["status"], "Entrada")

    def test_valor_negativo_da_ia_nao_vira_entrada(self):
        self.corrige({"valor": -40})
        self.assertEqual(self.gravado()["valor"], -40.0)

    def test_so_a_data(self):
        self.corrige({"data": "2026-11-20"}, "foi dia 20/11")
        self.assertEqual(self.gravado()["data"], "2026-11-20")
        self.assertEqual(self.gravado()["valor"], -50.0)

    def test_categoria_passa_pela_lista_do_usuario(self):
        self.corrige({"categoria": "lazer"}, "categoria lazer")
        self.assertEqual(self.gravado()["categoria"], "Lazer")
        self.corrige({"categoria": "Inventada"})
        self.assertEqual(self.gravado()["categoria"], "Outros")

    def test_nada_mudou_ou_invalido_continua_editando(self):
        for campos in ({}, {"valor": 50}, {"valor": "abc"}, {"valor": 0},
                       {"data": "2026-02-30"}, {"status": "gasto"}, {"id": 99}):
            self.assertEqual(self.corrige(campos), [mh.MSG_PECA_DE_NOVO], campos)
        self.p["atualizar_lancamento"].assert_not_called()
        self.p["salvar_edicao"].assert_not_called()

    def test_campos_que_nao_sao_dict(self):
        self.ia({"acao": "corrigir", "campos": "valor 40", "mensagem": ""})
        self.assertEqual(self.msg("40"), [mh.MSG_PECA_DE_NOVO])

    def test_lancamento_apagado_antes_de_chamar_a_ia(self):
        self.p["get_lancamento"].return_value = None
        self.assertEqual(self.msg("40 reais"), [mh.MSG_LANCAMENTO_SUMIU])
        self.p["extract_transaction"].assert_not_called()
        self.p["salvar_edicao"].assert_called_once_with(7, None)


class TestValorSoltoNaEdicao(BaseBot):
    """IA pediu dados no meio da edição, mas a mensagem era só um valor."""

    def setUp(self):
        super().setUp()
        self.row = usuario(edicao_gasto_id=42, edicao_iniciada_em=AGORA.isoformat())
        self.ia({"acao": "pedir_dados", "mensagem": "Beleza! Para registrar, me conta 👇"})

    def test_leitura_do_valor(self):
        casos = {"20 reiais": 20.0, "R$ 35,90": 35.9, "R$35": 35.0, "1.234,56 conto": 1234.56,
                 "35.90": 35.9, "1.500": 1500.0, "40": 40.0, "foi 12,5": 12.5,
                 "dia 20/11": None, "de 10 para 20": None, "muda isso": None, "0": None,
                 "foram 20 no mercado e não 10": None, "gastei 20 no mercado ontem à noite": None}
        for texto, esperado in casos.items():
            self.assertEqual(mh._valor_solto(texto), esperado, texto)

    def test_vinte_reiais_corrige_o_valor(self):
        r = self.msg("20 reiais")
        self.assertEqual(self.p["atualizar_lancamento"].call_args.kwargs["valor"], -20.0)
        self.assertIn("corrigido", r[0]["mensagem"])

    def test_sem_valor_pede_de_novo_sem_a_mensagem_padrao_de_registro(self):
        self.assertEqual(self.msg("muda isso"), [mh.MSG_PECA_DE_NOVO])
        self.p["atualizar_lancamento"].assert_not_called()


# ---------------------------------------------------------------------------
# Cota da Groq esgotada: avisar com clareza, nunca "erro interno"
# ---------------------------------------------------------------------------
class TestLimiteIA(BaseBot):

    def test_cota_do_dia_avisa_e_nao_registra(self):
        self.p["extract_transaction"].side_effect = mh.LimiteIA(diaria=True)
        self.assertEqual(self.msg("gastei 50 no mercado"), [mh.MSG_LIMITE_DIA])
        self.p["insert_transaction"].assert_not_called()

    def test_cota_por_minuto_pede_para_esperar(self):
        self.p["extract_transaction"].side_effect = mh.LimiteIA(diaria=False)
        self.assertEqual(self.msg("gastei 50 no mercado"), [mh.MSG_LIMITE_MINUTO])

    def test_cota_no_meio_da_edicao_nao_mexe_no_lancamento(self):
        self.row = usuario(edicao_gasto_id=42, edicao_iniciada_em=AGORA.isoformat())
        self.p["extract_transaction"].side_effect = mh.LimiteIA(diaria=True)
        self.assertEqual(self.msg("muda para 40"), [mh.MSG_LIMITE_DIA])
        self.p["atualizar_lancamento"].assert_not_called()
        self.p["salvar_edicao"].assert_not_called()  # segue editando: dá para tentar de novo

    def test_relatorio_sai_mesmo_sem_cota_para_as_dicas(self):
        self.ia({"acao": "relatorio", "mensagem": "", "formato": "opcao",
                 "periodo": {"data_inicio": "2026-10-27", "data_fim": "2026-10-28"}})
        self.p["get_transactions"].return_value = [GASTO]
        self.p["generate_financial_tips"].side_effect = mh.LimiteIA(diaria=True)
        r = self.msg("relatório de ontem")
        self.assertIn("Relatório Financeiro", r[0])
        self.assertEqual(r[1], mh.MSG_DICAS_SEM_COTA)


class TestTraducaoDo429(unittest.TestCase):
    """O 429 da Groq vira LimiteIA, e o texto do erro diz se é o teto do dia."""

    def _erro_429(self, texto):
        import httpx
        from groq import RateLimitError
        resp = httpx.Response(429, request=httpx.Request("POST", "https://api.groq.com"))
        return RateLimitError(texto, response=resp, body=None)

    def _chamar(self, texto):
        from tools import llm_router
        cliente = mock.Mock()
        cliente.chat.completions.create.side_effect = self._erro_429(texto)
        with self.assertRaises(llm_router.LimiteIA) as ctx:
            llm_router._chamar_groq(cliente, model="x", messages=[])
        return ctx.exception

    def test_teto_do_dia(self):
        e = self._chamar("Rate limit reached for model `openai/gpt-oss-120b` "
                         "on tokens per day (TPD): Limit 200000, Used 199000")
        self.assertTrue(e.diaria)

    def test_teto_por_minuto(self):
        e = self._chamar("Rate limit reached for model `openai/gpt-oss-120b` "
                         "on tokens per minute (TPM): Limit 8000, Used 7900")
        self.assertFalse(e.diaria)


if __name__ == "__main__":
    unittest.main()
