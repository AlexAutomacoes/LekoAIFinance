"""
Testes do importar extrato — sem rede, sem banco, sem IA.

    python -m unittest discover -s tests -v

Cobre a leitura do OFX (tools/extrato.py, módulo puro), a prévia e os botões
x|... (message_handler) e a entrada do arquivo pelo webhook (api/telegram.py).
"""
import io
import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_bot import BaseBot, usuario, TG, CATEGORIAS  # noqa: E402  (também prepara o env)

from tools import extrato  # noqa: E402
from tools import message_handler as mh  # noqa: E402


def ofx_sgml(transacoes, cartao=False, encoding="utf-8"):
    """Monta um OFX no estilo SGML (sem fechar as tags), o mais comum nos bancos daqui."""
    blocos = "".join(
        f"<STMTTRN>\n<TRNTYPE>OTHER\n<DTPOSTED>{d}120000[-3:BRT]\n<TRNAMT>{v}\n"
        f"<FITID>{fid}\n<MEMO>{memo}\n</STMTTRN>\n"
        for d, v, fid, memo in transacoes)
    tipo = "CCSTMTRS" if cartao else "STMTRS"
    texto = (f"OFXHEADER:100\nDATA:OFXSGML\nCHARSET:1252\n\n<OFX>\n<{tipo}>\n<BANKTRANLIST>\n"
             f"{blocos}</BANKTRANLIST>\n</{tipo}>\n</OFX>\n")
    return texto.encode(encoding)


EXTRATO = ofx_sgml([
    ("20261003", "-45.90", "A1", "IFOOD *RESTAURANTE"),
    ("20261005", "3500.00", "A2", "CREDITO SALARIO EMPRESA X"),
    ("20261007", "-120.00", "A3", "PIX ENVIADO JOAO"),
    ("20261010", "-89.90", "A4", "UBER *TRIP"),
])


# ---------------------------------------------------------------------------
# Leitura do OFX
# ---------------------------------------------------------------------------
class TestLeituraOfx(unittest.TestCase):

    def test_le_sgml_com_sinal_status_e_data(self):
        lido = extrato.ler_ofx(EXTRATO)
        self.assertFalse(lido["cartao"])
        t = lido["transacoes"]
        self.assertEqual(len(t), 4)
        self.assertEqual(t[0], {"contraparte": "", "status": "Saída", "valor": -45.9,
                                "descricao": "IFOOD *RESTAURANTE", "data": "2026-10-03",
                                "id_externo": "A1|2026-10-03|-45.90"})
        self.assertEqual(t[1]["status"], "Entrada")
        self.assertEqual(t[1]["valor"], 3500.0)

    def test_le_xml_com_tags_fechadas(self):
        xml = (b"<?xml version='1.0'?><OFX><BANKMSGSRSV1><STMTTRNRS><STMTRS><BANKTRANLIST>"
               b"<STMTTRN><TRNTYPE>DEBIT</TRNTYPE><DTPOSTED>20261001</DTPOSTED>"
               b"<TRNAMT>-10.50</TRNAMT><FITID>X9</FITID><NAME>PADARIA</NAME></STMTTRN>"
               b"</BANKTRANLIST></STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>")
        t = extrato.ler_ofx(xml)["transacoes"]
        self.assertEqual(len(t), 1)
        self.assertEqual(t[0]["descricao"], "PADARIA")
        self.assertEqual(t[0]["valor"], -10.5)

    def test_acento_em_windows_1252(self):
        conteudo = ofx_sgml([("20261001", "-30.00", "B1", "FARMÁCIA SÃO JOÃO")], encoding="cp1252")
        self.assertEqual(extrato.ler_ofx(conteudo)["transacoes"][0]["descricao"], "FARMÁCIA SÃO JOÃO")

    def test_valor_com_virgula(self):
        conteudo = ofx_sgml([("20261001", "-1.234,56", "C1", "ALUGUEL")])
        self.assertEqual(extrato.ler_ofx(conteudo)["transacoes"][0]["valor"], -1234.56)

    def test_fatura_de_cartao_pula_o_pagamento_da_fatura(self):
        conteudo = ofx_sgml([("20261001", "-50.00", "D1", "NETFLIX"),
                             ("20261002", "1500.00", "D2", "Pagamento recebido"),
                             ("20261003", "20.00", "D3", "ESTORNO LOJA")], cartao=True)
        lido = extrato.ler_ofx(conteudo)
        self.assertTrue(lido["cartao"])
        self.assertEqual([t["descricao"] for t in lido["transacoes"]], ["NETFLIX", "ESTORNO LOJA"])

    def test_linha_sem_valor_ou_data_e_pulada(self):
        conteudo = ofx_sgml([("20261001", "", "E1", "SEM VALOR"),
                             ("xx", "-5.00", "E2", "SEM DATA"),
                             ("20261002", "-5.00", "E3", "OK")])
        self.assertEqual([t["descricao"] for t in extrato.ler_ofx(conteudo)["transacoes"]], ["OK"])

    def test_arquivo_que_nao_e_ofx(self):
        with self.assertRaises(extrato.ExtratoInvalido):
            extrato.ler_ofx("data;valor\n01/10;-5".encode())

    def test_ofx_sem_transacoes(self):
        with self.assertRaises(extrato.ExtratoInvalido):
            extrato.ler_ofx(ofx_sgml([]))

    def test_arquivo_grande_demais(self):
        with self.assertRaises(extrato.ExtratoInvalido):
            extrato.ler_ofx(b"<OFX>" + b" " * (extrato.TAMANHO_MAXIMO_BYTES + 1))

    def test_transacoes_demais(self):
        muitas = [("20261001", "-1.00", f"F{i}", "X") for i in range(extrato.LIMITE_TRANSACOES + 1)]
        with self.assertRaises(extrato.ExtratoInvalido):
            extrato.ler_ofx(ofx_sgml(muitas))


class TestCategoriaPorRegra(unittest.TestCase):

    def test_regras_basicas(self):
        self.assertEqual(extrato.categoria_por_regra("IFOOD *X", "Saída"), "Alimentação")
        self.assertEqual(extrato.categoria_por_regra("UBER *TRIP", "Saída"), "Transporte")
        self.assertEqual(extrato.categoria_por_regra("Drogaria São Paulo", "Saída"), "Saúde")
        self.assertEqual(extrato.categoria_por_regra("CREDITO SALARIO", "Entrada"), "Salário")
        self.assertEqual(extrato.categoria_por_regra("PIX ENVIADO JOAO", "Saída"), "Outros")

    def test_entrada_nao_usa_regra_de_saida(self):
        # Um PIX recebido de alguém chamado "Mercado" não é gasto com Alimentação.
        self.assertEqual(extrato.categoria_por_regra("PIX RECEBIDO MERCADO LTDA", "Entrada"), "Outros")

    def test_categoria_do_usuario_escrita_na_descricao_ganha(self):
        self.assertEqual(extrato.categoria_por_regra("PETZ PETS LTDA", "Saída", ["Pets"]), "Pets")


def ofx_com_nome(transacoes):
    """Como o do Inter: MEMO com 'Cp :<código>-' e o NAME da contraparte."""
    blocos = "".join(
        f"<STMTTRN>\n<DTPOSTED>{d}\n<TRNAMT>{v}\n<FITID>{fid}\n<MEMO>{memo}\n<NAME>{nome}\n</STMTTRN>\n"
        for d, v, fid, memo, nome in transacoes)
    return f"<OFX>\n<STMTRS>\n<BANKTRANLIST>\n{blocos}</BANKTRANLIST>\n</STMTRS>\n</OFX>".encode()


# Formato do extrato do Inter, que motivou as regras. Valores, nomes e códigos
# são todos fictícios: extrato de cliente é dado pessoal e não vai para o Git.
INTER = ofx_com_nome([
    ("20261001", "500.00", "I1", 'Resgate: "CDB PORQUINHO BANCO X S A"', "Cdb Porquinho"),
    ("20261001", "-500.00", "I2", 'Pix enviado: "Cp :11111111-Ana Maria Souza"', "Ana Maria Souza"),
    ("20261002", "120.00", "I3", 'Pix recebido: "Cp :11111111-ANA MARIA SOUZA"', "Ana Maria Souza"),
    ("20261002", "-800.00", "I4", 'Aplicacao: "CDB CREDITO BANCO X S A"', "Cdb Credito"),
    ("20261007", "-300.00", "I5", 'Debito B3: "NOTA BOV 05/10/2026"', "Nota Bov"),
    ("20261015", "2000.00", "I6", 'Recebimento de proventos: "PAGAMENTO DE PROVENTOS EMPRESA"', "Empresa"),
    ("20261015", "10.00", "I7", 'Credito Evento B3: "* PROV * RENDIMENTO 100 ABCD11"', "Prov"),
    ("20261017", "-20.00", "I8", 'Pix enviado: "Cp :22222222-LANCHE BOM FOOD"', "Lanche Bom Food"),
    ("20261020", "-30.00", "I9", 'Pix enviado: "Cp :1-Joao Lima"', "Joao Lima"),
    ("20261021", "15.00", "I10", 'Pix recebido: "Cp :1-Joao Lima"', "Joao Lima"),
])


class TestExtratoReal(unittest.TestCase):

    def setUp(self):
        self.t = extrato.ler_ofx(INTER)["transacoes"]

    def test_descricao_sem_o_codigo_do_banco_nem_aspas(self):
        self.assertEqual(self.t[1]["descricao"], "Pix enviado: Ana Maria Souza")

    def test_aplicacao_resgate_e_pix_para_si_mesmo_ficam_de_fora(self):
        validas, internas = extrato.separar_internas(self.t, "Ana")
        self.assertEqual(sorted(t["id_externo"].split("|")[0] for t in internas),
                         ["I1", "I2", "I3", "I4"])
        self.assertEqual(len(validas), 6)

    def test_pix_nos_dois_sentidos_com_outra_pessoa_entra(self):
        # Dividir a conta com o João gera Pix indo e voltando, mas não é o usuário.
        validas, _ = extrato.separar_internas(self.t, "Ana")
        self.assertIn("Pix enviado: Joao Lima", [t["descricao"] for t in validas])

    def test_sem_o_nome_batendo_a_transferencia_entra(self):
        # Na dúvida, importa (dá para excluir); esconder um gasto real seria pior.
        _, internas = extrato.separar_internas(self.t, "Beto")
        self.assertEqual(sorted(t["id_externo"].split("|")[0] for t in internas), ["I1", "I4"])

    def test_categorias_do_extrato_real(self):
        cat = {t["id_externo"].split("|")[0]: extrato.categoria_por_regra(t["descricao"], t["status"])
               for t in self.t}
        self.assertEqual(cat["I5"], "Investimentos")   # compra na B3
        self.assertEqual(cat["I6"], "Salário")
        self.assertEqual(cat["I7"], "Investimentos")   # rendimento de FII
        self.assertEqual(cat["I8"], "Alimentação")


class TestDuplicados(unittest.TestCase):

    def test_mesmo_id_externo_e_repetido(self):
        t = extrato.ler_ofx(EXTRATO)["transacoes"]
        existentes = [{"id_externo": "A1|2026-10-03|-45.90", "data": "2026-10-03", "valor": -45.9}]
        novas, repetidas = extrato.separar_duplicados(t, existentes)
        self.assertEqual(len(novas), 3)
        self.assertEqual(repetidas[0]["id_externo"], "A1|2026-10-03|-45.90")

    def test_lancamento_manual_cobre_uma_transacao_so(self):
        t = [{"data": "2026-10-01", "valor": -8.0, "id_externo": "a"},
             {"data": "2026-10-01", "valor": -8.0, "id_externo": "b"}]
        existentes = [{"id_externo": None, "data": "2026-10-01", "valor": -8}]
        novas, repetidas = extrato.separar_duplicados(t, existentes)
        self.assertEqual((len(novas), len(repetidas)), (1, 1))


# ---------------------------------------------------------------------------
# Prévia e botões (message_handler)
# ---------------------------------------------------------------------------
PAGO = dict(plan_type="plus_monthly",
            subscription_expires_at=(datetime.now(timezone.utc) + timedelta(days=10)).isoformat())


class BaseImportacao(BaseBot):

    def setUp(self):
        super().setUp()
        self.row = usuario(**PAGO)
        for nome, valor in {
            "gastos_para_conferir_duplicados": [],
            "criar_importacao": 55,
            "get_importacao": None,
            "trocar_status_importacao": True,
            "inserir_importados": 3,
            "apagar_importados": 3,
        }.items():
            pat = mock.patch("tools.message_handler." + nome, return_value=valor)
            self.p[nome] = pat.start()
            self.addCleanup(pat.stop)
        self.baixar = mock.Mock(return_value=EXTRATO)

    def enviar(self, nome="extrato.ofx", tamanho=900):
        return mh.processar_extrato(nome, tamanho, self.baixar, TG, "Ana")

    def clicar(self, dados):
        return mh.tratar_clique_importacao(dados, TG, "Ana")

    def pendente(self, **extra):
        itens = [{"status": "Saída", "valor": -45.9, "descricao": "IFOOD", "data": "2026-10-03",
                  "id_externo": "A1|2026-10-03|-45.90", "categoria": "Alimentação"},
                 {"status": "Entrada", "valor": 3500.0, "descricao": "SALARIO", "data": "2026-10-05",
                  "id_externo": "A2|2026-10-05|3500.00", "categoria": "Outros"}]
        imp = {"id": 55, "status": "pendente", "itens": itens,
               "created_at": datetime.now(timezone.utc).isoformat()}
        imp.update(extra)
        self.p["get_importacao"].return_value = imp
        return imp


class TestEnvioDoArquivo(BaseImportacao):

    def test_gratis_e_bloqueado_antes_de_baixar(self):
        self.row = usuario()
        r = self.enviar()
        self.assertEqual(r[0]["tipo"], "botoes_planos")
        self.assertIn("extrato", r[0]["mensagem"])
        self.baixar.assert_not_called()
        self.p["criar_importacao"].assert_not_called()

    def test_plus_vencido_e_bloqueado(self):
        self.row = usuario(plan_type="plus_monthly",
                           subscription_expires_at=(datetime.now(timezone.utc) - timedelta(days=1)).isoformat())
        r = self.enviar()
        self.assertIn("venceu", r[0]["mensagem"])
        self.baixar.assert_not_called()

    def test_quem_nao_escolheu_plano_ve_o_menu(self):
        self.row = usuario(plano_escolhido_em=None)
        self.assertEqual(self.enviar()[0]["gratis"], "escolher")
        self.baixar.assert_not_called()

    def test_admin_no_gratis_passa(self):
        self.row = usuario()
        with mock.patch.dict(os.environ, {"ADMIN_TELEGRAM_IDS": str(TG)}):
            r = self.enviar()
        self.assertEqual(r[0]["tipo"], "botoes")

    def test_extensao_errada_explica_como_exportar(self):
        r = self.enviar(nome="extrato.pdf")
        self.assertIn("OFX", r[0])
        self.baixar.assert_not_called()

    def test_arquivo_grande_nao_e_baixado(self):
        r = self.enviar(tamanho=extrato.TAMANHO_MAXIMO_BYTES + 1)
        self.assertIn("grande demais", r[0])
        self.baixar.assert_not_called()

    def test_previa_nao_grava_lancamento(self):
        r = self.enviar()
        self.p["insert_transaction"].assert_not_called()
        self.p["inserir_importados"].assert_not_called()
        user_id, arquivo, itens = self.p["criar_importacao"].call_args[0]
        self.assertEqual((user_id, arquivo, len(itens)), (7, "extrato.ofx", 4))
        # Categorias da lista do usuário (a "Salário" não existe nela -> "Outros").
        self.assertEqual([t["categoria"] for t in itens],
                         ["Alimentação", "Outros", "Outros", "Outros"])
        msg = r[0]["mensagem"]
        self.assertIn("4 lançamento(s)", msg)
        self.assertIn("Nada foi gravado", msg)
        self.assertEqual(r[0]["botoes"][0][0]["dados"], "x|ok|55")

    def test_repetidos_sao_pulados_e_avisados(self):
        self.p["gastos_para_conferir_duplicados"].return_value = [
            {"id_externo": "A1|2026-10-03|-45.90", "data": "2026-10-03", "valor": -45.9}]
        r = self.enviar()
        self.assertEqual(len(self.p["criar_importacao"].call_args[0][2]), 3)
        self.assertIn("1 já estava(m) lançada(s)", r[0]["mensagem"])

    def test_tudo_repetido_nao_cria_previa(self):
        lido = extrato.ler_ofx(EXTRATO)["transacoes"]
        self.p["gastos_para_conferir_duplicados"].return_value = lido
        r = self.enviar()
        self.assertIn("todas já estavam lançadas", r[0])
        self.p["criar_importacao"].assert_not_called()

    def test_movimentacao_interna_fica_fora_e_e_avisada(self):
        self.baixar.return_value = INTER
        r = self.enviar()
        itens = self.p["criar_importacao"].call_args[0][2]
        self.assertEqual(len(itens), 6)
        self.assertNotIn("contraparte", itens[0])  # não vai para o banco
        self.assertIn("4 movimentação(ões)", r[0]["mensagem"])

    def test_extrato_so_com_movimentacao_interna(self):
        self.baixar.return_value = ofx_com_nome([
            ("20261001", "100.00", "R1", "Resgate: CDB", "Cdb"),
            ("20261001", "-100.00", "R2", "Aplicacao: CDB", "Cdb")])
        r = self.enviar()
        self.assertIn("movimentações entre suas contas", r[0])
        self.p["criar_importacao"].assert_not_called()

    def test_ofx_invalido_responde_sem_quebrar(self):
        self.baixar.return_value = b"isso nao e um ofx"
        r = self.enviar()
        self.assertIn("não parece um extrato", r[0])

    def test_falha_no_download_responde_sem_quebrar(self):
        self.baixar.side_effect = RuntimeError("timeout")
        self.assertEqual(self.enviar(), [mh.MSG_FALHA_EXTRATO])

    def test_falha_nas_categorias_nao_impede_a_previa(self):
        self.p["listar_categorias"].side_effect = RuntimeError("banco fora")
        r = self.enviar()
        self.assertEqual(r[0]["tipo"], "botoes")
        self.assertEqual(self.p["criar_importacao"].call_args[0][2][1]["categoria"], "Salário")


class TestBotoesImportacao(BaseImportacao):

    def test_importar_grava_e_oferece_desfazer(self):
        self.pendente()
        r = self.clicar("x|ok|55")
        self.p["inserir_importados"].assert_called_once()
        self.assertEqual(self.p["inserir_importados"].call_args[0][0], 7)
        trocas = [c.args[2:4] for c in self.p["trocar_status_importacao"].call_args_list]
        self.assertEqual(trocas, [("pendente", "importando"), ("importando", "importada")])
        self.assertIn("Importei 3", r[0]["mensagem"])
        self.assertEqual(r[0]["botoes"][0][0]["dados"], "x|undo|55")

    def test_clique_duplo_nao_importa_duas_vezes(self):
        self.pendente()
        self.p["trocar_status_importacao"].return_value = False  # outro update já pegou
        r = self.clicar("x|ok|55")
        self.p["inserir_importados"].assert_not_called()
        self.assertIn("já está sendo importado", r[0])

    def test_importar_pula_o_que_entrou_depois_da_previa(self):
        self.pendente()
        self.p["gastos_para_conferir_duplicados"].return_value = [
            {"id_externo": "A1|2026-10-03|-45.90", "data": "2026-10-03", "valor": -45.9}]
        self.clicar("x|ok|55")
        itens = self.p["inserir_importados"].call_args[0][2]
        self.assertEqual([t["descricao"] for t in itens], ["SALARIO"])

    def test_falha_ao_gravar_volta_para_pendente(self):
        self.pendente()
        self.p["inserir_importados"].side_effect = RuntimeError("banco fora")
        r = self.clicar("x|ok|55")
        ultima = self.p["trocar_status_importacao"].call_args_list[-1].args[2:4]
        self.assertEqual(ultima, ("importando", "pendente"))
        self.assertIn("Não consegui importar", r[0])

    def test_plano_venceu_entre_previa_e_clique(self):
        self.pendente()
        self.row = usuario()
        r = self.clicar("x|ok|55")
        self.assertEqual(r[0]["tipo"], "botoes_planos")
        self.p["inserir_importados"].assert_not_called()

    def test_ja_importada_nao_importa_de_novo(self):
        self.pendente(status="importada")
        self.assertIn("já foi importado", self.clicar("x|ok|55")[0])
        self.p["inserir_importados"].assert_not_called()

    def test_previa_expirada(self):
        self.pendente(created_at=(datetime.now(timezone.utc) - timedelta(hours=25)).isoformat())
        self.assertIn("expirou", self.clicar("x|ok|55")[0])
        self.p["inserir_importados"].assert_not_called()

    def test_id_forjado_de_outra_pessoa(self):
        # get_importacao filtra por user_id: de outra pessoa volta None.
        self.assertEqual(self.clicar("x|ok|999"), [mh.MSG_IMPORTACAO_SUMIU])
        self.p["get_importacao"].assert_called_once_with(7, 999)

    def test_botao_malformado(self):
        self.assertIn("Não entendi", self.clicar("x|ok|abc")[0])

    def test_ver_lista(self):
        self.pendente()
        r = self.clicar("x|ver|55")
        self.assertIn("IFOOD", r[0]["mensagem"])
        self.assertIn("-R$ 45,90", r[0]["mensagem"])
        self.p["inserir_importados"].assert_not_called()

    def test_cancelar_limpa_a_previa(self):
        self.pendente()
        r = self.clicar("x|no|55")
        c = self.p["trocar_status_importacao"].call_args
        self.assertEqual(c.args[2:4], ("pendente", "cancelada"))
        self.assertTrue(c.kwargs["limpar_itens"])
        self.assertIn("não importei nada", r[0])

    def test_desfazer_pede_confirmacao_e_depois_apaga(self):
        self.pendente(status="importada", itens=[])
        r = self.clicar("x|undo|55")
        self.p["apagar_importados"].assert_not_called()
        self.assertEqual(r[0]["botoes"][0][0]["dados"], "x|undook|55")
        r = self.clicar("x|undook|55")
        self.p["apagar_importados"].assert_called_once_with(7, 55)
        self.assertIn("apaguei 3", r[0])

    def test_desfazer_funciona_mesmo_sem_plano_pago(self):
        self.row = usuario()
        self.pendente(status="importada", itens=[])
        self.clicar("x|undook|55")
        self.p["apagar_importados"].assert_called_once()

    def test_desfazer_duas_vezes(self):
        self.pendente(status="importada", itens=[])
        self.p["trocar_status_importacao"].return_value = False
        self.assertIn("já foi desfeita", self.clicar("x|undook|55")[0])
        self.p["apagar_importados"].assert_not_called()


# ---------------------------------------------------------------------------
# api/telegram.py: o arquivo chega pelo webhook
# ---------------------------------------------------------------------------
class TestWebhookArquivo(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from api import telegram as tg
        cls.tg = tg

    def _post(self, update):
        h = object.__new__(self.tg.handler)
        raw = json.dumps(update).encode()
        h.headers = {"Content-Length": str(len(raw))}
        h.rfile, h.wfile, h.path = io.BytesIO(raw), io.BytesIO(), "/api/telegram"
        h.respostas = []
        h.send_response = h.respostas.append
        h.send_header = lambda *a: None
        h.end_headers = lambda: None
        with mock.patch.object(self.tg, "WEBHOOK_SECRET", None), \
             mock.patch.object(self.tg, "enviar_respostas") as enviar:
            h.do_POST()
        return h, enviar

    def test_documento_vai_para_o_processar_extrato(self):
        upd = {"message": {"chat": {"id": TG}, "from": {"id": TG, "first_name": "Ana"},
                           "document": {"file_id": "F1", "file_name": "nubank.ofx", "file_size": 800}}}
        with mock.patch("tools.message_handler.processar_extrato", return_value=["ok"]) as proc, \
             mock.patch.object(self.tg, "baixar_arquivo", return_value=b"x") as baixar:
            h, enviar = self._post(upd)
            nome, tamanho, baixar_fn, tg_id, primeiro = proc.call_args[0]
            self.assertEqual((nome, tamanho, tg_id, primeiro), ("nubank.ofx", 800, TG, "Ana"))
            baixar.assert_not_called()  # o download fica a cargo do handler, depois do gate
            self.assertEqual(baixar_fn(), b"x")
            baixar.assert_called_once_with("F1")
        enviar.assert_called_once_with(TG, ["ok"])
        self.assertEqual(h.respostas, [200])

    def test_clique_x_vai_para_o_handler_de_importacao(self):
        upd = {"callback_query": {"id": "1", "data": "x|ok|55", "from": {"id": TG, "first_name": "Ana"},
                                  "message": {"chat": {"id": TG}}}}
        with mock.patch.object(self.tg, "answer_callback_query"), \
             mock.patch("tools.message_handler.tratar_clique_importacao", return_value=["ok"]) as clique:
            h, _ = self._post(upd)
        clique.assert_called_once_with("x|ok|55", TG, "Ana")
        self.assertEqual(h.respostas, [200])

    def test_mensagem_de_texto_continua_indo_para_o_process_message(self):
        upd = {"message": {"chat": {"id": TG}, "from": {"id": TG, "first_name": "Ana"}, "text": "oi"}}
        with mock.patch.object(self.tg, "process_message", return_value=["olá"]) as pm, \
             mock.patch("tools.message_handler.processar_extrato") as proc:
            self._post(upd)
        pm.assert_called_once_with("oi", TG, "Ana")
        proc.assert_not_called()

    def test_foto_e_sticker_continuam_ignorados(self):
        upd = {"message": {"chat": {"id": TG}, "from": {"id": TG}, "photo": [{"file_id": "P"}]}}
        with mock.patch.object(self.tg, "process_message") as pm, \
             mock.patch("tools.message_handler.processar_extrato") as proc:
            h, enviar = self._post(upd)
        pm.assert_not_called()
        proc.assert_not_called()
        enviar.assert_not_called()
        self.assertEqual(h.respostas, [200])

    def test_erro_no_extrato_ainda_responde_200(self):
        upd = {"message": {"chat": {"id": TG}, "from": {"id": TG},
                           "document": {"file_id": "F1", "file_name": "a.ofx"}}}
        with mock.patch("tools.message_handler.processar_extrato", side_effect=RuntimeError("x")):
            h, _ = self._post(upd)
        self.assertEqual(h.respostas, [200])

    def test_baixar_arquivo_usa_get_file_e_depois_o_download(self):
        r1 = mock.Mock()
        r1.json.return_value = {"result": {"file_path": "documents/file_1.ofx"}}
        r2 = mock.Mock(content=b"<OFX>")
        with mock.patch.object(self.tg.httpx, "get", side_effect=[r1, r2]) as get:
            self.assertEqual(self.tg.baixar_arquivo("F1"), b"<OFX>")
        self.assertTrue(get.call_args_list[0].args[0].endswith("/getFile"))
        self.assertTrue(get.call_args_list[1].args[0].endswith("/documents/file_1.ofx"))


if __name__ == "__main__":
    unittest.main()
