#!/usr/bin/env python3

import datetime
import io
import json
import hashlib
import logging
import os
import sys
import re

from os.path import abspath, basename, dirname, join as pjoin
from collections import namedtuple, OrderedDict

import requests_cache
import lxml.html

ONE_HOUR = datetime.timedelta(hours=1)


class RequestsWrapper():
    def __init__(self):
        self.session = requests_cache.core.CachedSession(expire_after=ONE_HOUR)

    def get(self, url, *args, **kwargs):
        return self.session.get(url, *args, **kwargs)


def main(output_dir=None):
    logging.basicConfig(level=logging.DEBUG)
    output_dir = output_dir or abspath(pjoin(dirname(__file__), 'output'))
    scraper = ICOPenaltyScraper(output_dir, RequestsWrapper())

    scraper.run()


class ICOPenaltyScraper():
    BASE_URL = 'https://ico.org.uk'
    LIST_URL = '{}/action-weve-taken/enforcement/'.format(BASE_URL)  # noqa

    XPATH_LIST_PAGE_LINK = '//a[contains(@href, "/action-weve-taken/enforcement/")]'  # noqa
    XPATH_PDF_LINK = "//a[contains(@href, '/media/action-weve-taken') and contains(@href, '.pdf')]"  # noqa
    XPATH_DATE = "//dt[contains(text(), 'Date')]/following-sibling::dd[1]"  # noqa

    PDF = namedtuple('PDF', 'id,url,type,date,title,filename,sha256')

    def __init__(self, output_dir, requests_like_object):
        self.output_dir = output_dir
        self.http = requests_like_object
        self.penalty_pages = None
        self.penalty_pdfs = None

    def run(self):
        self.mkdir_p(pjoin(self.output_dir, 'pdfs'))
        self.parse_list_page()
        self.parse_pdf_urls_from_penalty_pages()
        self.download_pdfs()
        self.write_index_json()
        self.write_metadata_json()

    @staticmethod
    def mkdir_p(directory):
        if not os.path.isdir(directory):
            os.makedirs(directory)
        return directory

    def parse_list_page(self):
        root = self._get_as_lxml(self.LIST_URL)

        self.penalty_pages = [
            self._expand_href(a.attrib['href']) for a in root.xpath(
                self.XPATH_LIST_PAGE_LINK)
        ]

    def parse_pdf_urls_from_penalty_pages(self):

        self.penalty_pdfs = list(
            filter(None, map(
                self.parse_pdf_url_from_penalty_page,
                self.penalty_pages
            ))
        )

    def parse_pdf_url_from_penalty_page(self, url):
        """
        Return a PDF() object for PDF URL linked in the penalty page.
        """
        root = self._get_as_lxml(url)
        pdf_url = self._parse_pdf_url(root, url)

        if pdf_url is None:
            return None

        return self.PDF(
            id=self._parse_id(pdf_url),
            url=pdf_url,
            type=self._parse_type(pdf_url),
            date=self._parse_date(root),
            title=self._parse_title(root),
            filename=basename(pdf_url),
            sha256=None
        )

    def _parse_pdf_url(self, lxml_root, url):
        a_tags = lxml_root.xpath(self.XPATH_PDF_LINK)

        if len(a_tags) == 0:
            logging.warn('No PDF on page {}'.format(url))

        elif len(a_tags) == 1:
            return self._expand_href(a_tags[0].attrib['href'])

        else:
            raise RuntimeError('Multiple PDF links: on page {} {}'.format(
                url, a_tags))

    def _parse_title(self, lxml_root):
        h1s = lxml_root.xpath('//h1')
        if len(h1s) == 1:
            return h1s[0].text_content().strip()

    def _parse_date(self, lxml_root):
        def parse(date_string):
            "e.g. 21 December 2017"
            return datetime.datetime.strptime(
                date_string, '%d %B %Y'
            ).date().isoformat()

        dates = lxml_root.xpath(self.XPATH_DATE)
        if len(dates) == 1:
            return parse(dates[0].text_content().strip())

    def _parse_id(self, pdf_url):
        match = re.search('\/(?P<id>\d+)\/', pdf_url)
        if match:
            return match.group('id')

    def _parse_type(self, pdf_url):
        match = re.search('\/action-weve-taken\/(?P<type>.+?)\/', pdf_url)
        if match:
            type_slug = match.group('type')

            return {
                'enforcement-notices': 'enforcement-notice',
                'mpns': 'monetary-penalty',
                'undertakings': 'undertaking',
            }.get(type_slug, None)

    def download_pdfs(self):
        def download_and_add_sha(pdf):
            logging.info('Downloading {}'.format(pdf.url))

            full_filename = pjoin(self.output_dir, 'pdfs', pdf.filename)

            with io.open(full_filename, 'wb') as f:
                response = self.http.get(pdf.url)
                response.raise_for_status()
                f.write(response.content)
                f.flush()

            data = pdf._asdict()
            data.update({'sha256': self.sha256(full_filename)})

            return self.PDF(**data)

        self.penalty_pdfs = list(
            map(
                download_and_add_sha,
                self.penalty_pdfs
            )
        )

    def write_index_json(self):

        def to_dict(pdf):
            return OrderedDict([
                ('id', pdf.id),
                ('url', pdf.url),
                ('type', pdf.type),
                ('date', pdf.date),
                ('title', pdf.title),
                ('filename', pdf.filename),
                ('sha256', pdf.sha256),
            ])

        def key(pdf):
            return int(pdf.id)

        pdfs = [to_dict(p) for p in sorted(self.penalty_pdfs, key=key)]

        index = OrderedDict([
            ('pdfs', pdfs),
        ])

        with io.open(pjoin(self.output_dir, 'index.json'), 'w') as f:
            json.dump(index, f, indent=4)

    def write_metadata_json(self):

        metadata = OrderedDict([
            ('last_updated', datetime.datetime.now().isoformat())
        ])

        with io.open(pjoin(self.output_dir, 'metadata.json'), 'w') as f:
            json.dump(metadata, f, indent=4)

    def _get_as_lxml(self, url):
        logging.info(url)
        response = self.http.get(url)
        response.raise_for_status()

        if response.status_code == 301:
            raise RuntimeError(response.headers)

        return lxml.html.fromstring(response.text)

    def _expand_href(self, href):
        if href.startswith('/'):  # not complete
            return '{}{}'.format(self.BASE_URL, href)
        else:
            return href

    @staticmethod
    def sha256(filename):
        hasher = hashlib.sha256()
        with io.open(filename, 'rb') as afile:
            buf = afile.read()
            hasher.update(buf)

        return hasher.hexdigest()


if __name__ == '__main__':
    main(*sys.argv[1:])
