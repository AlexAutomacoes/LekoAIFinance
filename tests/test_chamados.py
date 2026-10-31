"""
Testes do aviso ao cliente quando o chamado é resolvido pelo dashboard.
Sem rede: Supabase e Telegram são dublês.

    python -m unittest discover -s tests -v
"""
import os
import sys
import unittest
import urllib.error
import urllib.parse
from unittest import mock

os.environ.setdefault("SUPABASE_URL", "https://exemplo.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "teste")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools import chamados_service as cs  # noqa: E402

CLIENTE = {"id": 31, "status": "resolvido", "test_name": "telegram:8071209864 (Felipe)",
           "resolution_note": "Já implementado! Os botões dos planos aparecem no início."}


class TestPatchAvisaCliente(unittest.TestCase):

    def setUp(self):
        self.linha = dict(CLIENTE)
        tabela = mock.MagicMock()
        tabela.update.return_value.eq.return_value.execute.side_effect = \
            lambda: mock.Mock(data=[self.linha] if self.linha else [])
        cliente = mock.MagicMock()
        cliente.table.return_value = tabela
        self.tabela = tabela
        p1 = mock.patch.object(cs, "_client", return_value=cliente)
        p2 = mock.patch.object(cs, "auth_error", return_value=None)  # autenticado
        p3 = mock.patch("urllib.request.urlopen", side_effect=self._telegram)
        p4 = mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "TOKEN"})
        for p in (p1, p2, p3, p4):
            p.start()
            self.addCleanup(p.stop)
        self.enviados = []
        self.telegram_falha = False

    def _telegram(self, req, timeout=None):
        if self.telegram_falha:
            raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", {}, None)
        self.enviados.append(dict(urllib.parse.parse_qsl(req.data.decode())))
        return mock.MagicMock()

    def patch(self, **body):
        return cs.patch({}, {"id": 31, **body})

    def test_resolver_pelo_dashboard_avisa_o_cliente(self):
        status, corpo = self.patch(status="resolvido", notificar_cliente=True,
                                   resolution_note=CLIENTE["resolution_note"])
        self.assertEqual(status, 200)
        self.assertTrue(corpo["cliente_notificado"])
        self.assertEqual(len(self.enviados), 1)
        msg = self.enviados[0]
        self.assertEqual(msg["chat_id"], "8071209864")
        self.assertIn("Saudações Felipe!", msg["text"])
        self.assertIn("nº31", msg["text"])
        self.assertIn("Já implementado!", msg["text"])  # a nota vai junto

    def test_sem_nota_manda_so_o_aviso(self):
        self.linha = dict(CLIENTE, resolution_note=None)
        self.patch(status="resolvido", notificar_cliente=True)
        self.assertIn("já foi resolvido.", self.enviados[0]["text"])

    def test_n1_automatico_nao_gera_mensagem_duplicada(self):
        # o N1 manda a própria mensagem e não envia notificar_cliente
        status, corpo = self.patch(status="resolvido", resolution_note="[N1 resolveu] x")
        self.assertEqual(status, 200)
        self.assertEqual(self.enviados, [])
        self.assertNotIn("cliente_notificado", corpo)

    def test_so_avisa_ao_resolver(self):
        for st in ("atendendo", "aberto"):
            self.patch(status=st, notificar_cliente=True)
        self.patch(nivel="n2", status="aberto", notificar_cliente=True)
        self.assertEqual(self.enviados, [])

    def test_chamado_do_ci_nao_tem_para_quem_avisar(self):
        self.linha = dict(CLIENTE, test_name="health_check")
        status, corpo = self.patch(status="resolvido", notificar_cliente=True)
        self.assertEqual(status, 200)
        self.assertFalse(corpo["cliente_notificado"])
        self.assertEqual(self.enviados, [])

    def test_falha_do_telegram_aparece_e_nao_desfaz_o_resolvido(self):
        self.telegram_falha = True
        status, corpo = self.patch(status="resolvido", notificar_cliente=True)
        self.assertEqual(status, 200)
        self.assertFalse(corpo["cliente_notificado"])
        self.tabela.update.assert_called_once()

    def test_sem_token_do_bot(self):
        with mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": ""}):
            _, corpo = self.patch(status="resolvido", notificar_cliente=True)
        self.assertFalse(corpo["cliente_notificado"])

    def test_chamado_inexistente(self):
        self.linha = None
        status, _ = self.patch(status="resolvido", notificar_cliente=True)
        self.assertEqual(status, 404)
        self.assertEqual(self.enviados, [])

    def test_sem_autenticacao_nao_grava_nem_avisa(self):
        with mock.patch.object(cs, "auth_error", return_value=(401, {"error": "x"})):
            status, _ = self.patch(status="resolvido", notificar_cliente=True)
        self.assertEqual(status, 401)
        self.tabela.update.assert_not_called()
        self.assertEqual(self.enviados, [])


class TestClienteDoChamado(unittest.TestCase):

    def test_formatos(self):
        casos = {
            "telegram:8071209864 (Felipe)": (8071209864, "Felipe"),
            "telegram:123 (Ana Maria)": (123, "Ana Maria"),
            "telegram:123": (123, None),
            "telegram:abc (X)": (None, "X"),
            "code_review": (None, None),
            "": (None, None),
        }
        for tn, esperado in casos.items():
            self.assertEqual(cs._cliente_do_chamado({"test_name": tn}), esperado, tn)
        self.assertEqual(cs._cliente_do_chamado({}), (None, None))

    def test_mensagem_sem_nome(self):
        txt = cs.mensagem_resolvido({"id": 5, "resolution_note": ""}, None)
        self.assertTrue(txt.startswith("Saudações!"))


if __name__ == "__main__":
    unittest.main()
