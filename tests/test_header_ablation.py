"""Regression coverage for isolated header-ablation cases; no Google calls."""
import importlib.util
from pathlib import Path
import unittest
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / 'deploy/image-test/header_ablation.py'
spec = importlib.util.spec_from_file_location('header_ablation', SCRIPT)
ablation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ablation)


class HeaderAblationIsolationTests(unittest.TestCase):
    def test_anonymous_response_state_does_not_poison_next_control(self):
        experiment = ablation.Experiment.__new__(ablation.Experiment)
        experiment.wait = mock.Mock()
        experiment.publish = mock.Mock()
        experiment.case_id = 0
        experiment.root = mock.MagicMock()
        (experiment.root / 'STOP').exists.return_value = False
        experiment.rpc = {'url': 'https://gemini.google.com/rpc'}
        experiment.headers = {'rpc': {}, 'mediator': {}}
        experiment.load_current_cookie = mock.Mock(side_effect=[
            ('login=first', None), ('login=second', None), ('login=third', None),
        ])
        sessions = []
        observations = []

        def new_session():
            session = mock.Mock()
            session.response_jar = {}
            sessions.append(session)
            return session

        def request(stage, url, omitted=None):
            session = experiment.session
            observations.append((experiment.case_id, stage, session,
                                 dict(session.response_jar),
                                 experiment.headers['rpc']['cookie'],
                                 experiment.headers['mediator']['cookie']))
            if omitted == 'cookie':
                session.response_jar['anonymous'] = 'anonymous-response-state'
                return False, None
            session.response_jar['chain_step'] = stage
            return True, {
                'rpc': 'https://lh3.googleusercontent.com/gg/source',
                'source': 'https://lh3.google.com/rd-gg/mediator',
                'mediator': 'https://lh3.googleusercontent.com/rd-gg/final',
                'final': None,
            }[stage]

        experiment.make_session = new_session
        experiment.request = request
        self.assertEqual(experiment.case(), (True, ''))
        self.assertEqual(experiment.case('cookie'), (False, 'rpc'))
        self.assertEqual(experiment.case(), (True, ''))
        self.assertEqual(len(sessions), 3)
        for index, expected_cookie in enumerate(('login=first', 'login=second', 'login=third'), 1):
            case = [row for row in observations if row[0] == index]
            self.assertEqual(case[0][3], {})
            self.assertTrue(all(row[2] is sessions[index - 1] for row in case))
            self.assertTrue(all(row[4:6] == (expected_cookie, expected_cookie) for row in case))
            sessions[index - 1].close.assert_called_once()
        self.assertNotIn('anonymous', observations[-1][3])
        self.assertIsNone(experiment.session)
        self.assertEqual(experiment.load_current_cookie.call_count, 3)

    def test_session_closes_when_a_chain_is_interrupted(self):
        experiment = ablation.Experiment.__new__(ablation.Experiment)
        experiment.wait = mock.Mock()
        experiment.publish = mock.Mock()
        experiment.case_id = 0
        experiment.root = mock.MagicMock()
        (experiment.root / 'STOP').exists.return_value = False
        experiment.rpc = {'url': 'https://gemini.google.com/rpc'}
        experiment.headers = {'rpc': {}, 'mediator': {}}
        experiment.load_current_cookie = mock.Mock(return_value=('login=current', None))
        session = mock.Mock()
        experiment.make_session = mock.Mock(return_value=session)
        experiment.request = mock.Mock(side_effect=ablation.StopExperiment('rate_limit_or_challenge'))
        with self.assertRaises(ablation.StopExperiment):
            experiment.case()
        session.close.assert_called_once()
        self.assertIsNone(experiment.session)


if __name__ == '__main__':
    unittest.main()
