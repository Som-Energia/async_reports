# -*- coding: utf-8 -*-
import time
import netsvc
from osv import osv
from service import security
from tools import config
from os.path import join as pj


def log(msg, level=netsvc.LOG_INFO):
    logger = netsvc.Logger()
    logger.notifyChannel('async_reports', level, msg)


def get_template(template):
    templates = pj(config['addons_path'], 'async_reports', 'templates')
    with open(pj(templates, template)) as template:
        return template.read()


def do_GET(self):
    from werkzeug.routing import Map, Rule
    from werkzeug.exceptions import NotFound, MethodNotAllowed
    from oorq.oorq import setup_redis_connection
    from jinja2 import Template
    from rq import cancel_job
    from rq.job import Job
    from rq.exceptions import NoSuchJobError
    import times

    setup_redis_connection()
    m = Map([Rule('/job/<string:job>', endpoint='job'),
             Rule('/job/<string:job>/download', endpoint='download'),
             Rule('/job/<string:job>/cancel', endpoint='cancel')])
    urls = m.bind('')
    try:
        endpoint, params = urls.match(self.path)
        job = Job.fetch(params['job'])
        self.send_response(200)
        if endpoint in ('job', 'cancel'):
            running_time = times.now() - job.enqueued_at
            if endpoint == 'cancel':
                cancel_job(job.id)
            self.send_header('Content-Type', 'text/html')
            self.end_headers()
            content = Template(get_template('jobs.html'))
            self.wfile.write(content.render(job=job, rt=running_time))
        elif endpoint == 'download' and job.status == 'finished':
            self.send_header('Content-Type', 'application/%s' % job.meta['format'])
            self.send_header('Content-Length', len(job.result[0]))
            self.send_header('Content-Disposition', 'attachment;'
                            'filename=report.%s' % job.meta['format'])
            self.end_headers()
            self.wfile.write(job.result[0])
    except (NotFound, NoSuchJobError):
        self.send_response(404)
        self.end_headers()
    except MethodNotAllowed:
        self.send_response(405)
        self.end_headers()


def monkeypatch_request_handler():
    netsvc.SimpleXMLRPCRequestHandler.do_GET = do_GET
    log('Monkey patching SimpleXMLRPCRequestHandler...')


def async_report_report(db, uid, passwd, object, ids, datas=None, context=None):
    from oorq.oorq import setup_redis_connection
    from oorq.tasks import report
    from rq import Queue
    from jinja2 import Template
    if not datas:
        datas = {}
    if not context:
        context={}
    security.check(db, uid, passwd)
    self = netsvc.SERVICES['report']

    self.id_protect.acquire()
    self.id += 1
    id = self.id
    self.id_protect.release()

    self._reports[id] = {
        'uid': uid,
        'result': False,
        'state': False,
        'exception': None
    }
    redis_conn = setup_redis_connection()
    q = Queue('report', default_timeout=86400,
              connection=redis_conn)
    # Pass OpenERP server config to the worker
    conf_attrs = dict(
        [(attr, value) for attr, value in config.options.items()]
    )
    job = q.enqueue(report, conf_attrs, db, uid, object, ids, datas, context)
    job.result_ttl = 86400
    job.save()
    # Check the configured timeout for the report. If the timeout is reached
    # then return a html report which redirects to the job info page.
    timeout = int(config.get('report_timeout', 5))
    protocol = 'http'
    if config['secure']:
        protocol = 'https'
    result = job.result
    while not result:
        time.sleep(0.1)
        result = job.result
        timeout -= 0.1
        if timeout <= 0:
            tmpl = Template(get_template('async_reports.html'))
            result = (tmpl.render(protocol=protocol,
                                  host=config['interface'],
                                  port=config['port'],
                                  job=job.id),
                      'html')
    self._reports[id]['result'] = result[0]
    self._reports[id]['format'] = result[1]
    self._reports[id]['state'] = True
    return id


def monkeypatch_report_go():
    # from service.web_services import report_spool
    # report_spool.report = async_report_report
    report = netsvc.SERVICES['report']
    report._methods['report'] = async_report_report
    log('Monkey patching report...')


class AsyncReports(osv.osv):
    _name = 'async.reports'
    _auto = False

    def __init__(self, pool, cursor):
        monkeypatch_request_handler()
        monkeypatch_report_go()
        super(AsyncReports, self).__init__(pool, cursor)

AsyncReports()
