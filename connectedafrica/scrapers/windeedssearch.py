import argparse
from itertools import chain
import json
import re
from urlparse import urljoin

from lxml import html
from Levenshtein import jaro
from scrapekit.tasks import Task

from connectedafrica.scrapers import windeeds
from connectedafrica.scrapers.util import (MultiCSV, gdocs_persons,
                                           normalize_string)


DIRECTOR_SEARCH_URL = urljoin(windeeds.URL, '/Cipc/CipcDirectorSearch')
DIRECTOR_SELECT_URL = urljoin(windeeds.URL, '/Cipc/DirectorListSearchOffOfInput?Length=4')


class SearchedPerson(object):
    description_re = re.compile(
        ur'^(?P<last_name>.*),\s*(?P<first_name>.*),\s*(?P<id>\d+)$',
        flags=re.UNICODE
    )

    def __init__(self, url, description):
        self.url = url
        self.description = description
        self.national_id = None
        self.first_name = None
        self.last_name = None

        match = self.description_re.match(description)
        if match:
            self.national_id = match.group('id')
            self.first_name = match.group('first_name')
            self.last_name = match.group('last_name')

    def __hash__(self):
        return self.url.__hash__()

    def __repr__(self):
        return '<%s(first_name=%r, last_name=%r, id=%r)>' % \
                (self.__class__.__name__, self.first_name,
                 self.last_name, self.national_id)


class SearchedPersonNotFound(SearchedPerson):

    def __init__(self, url, description):
        super(SearchedPersonNotFound, self).__init__(url, description)

        if not self.description_re.match(description):
            parts = description.split(', ')
            if re.match(ur'^\d+$', parts[-1]):
                self.national_id = parts[-1]
                names = parts[0:-1]
            else:
                names = parts
            if len(names) >= 2:
                self.first_name = ', '.join(names[1:])
            if len(names) >= 1:
                self.last_name = names[0]


persons = set()
persons_by_id = {}
persons_by_last_name = {}


def record_searchedperson(person):
    if person.national_id:
        persons_by_id[person.national_id] = person

    if person not in persons and person.last_name:
        last_name_norm = normalize_string(person.last_name)
        persons_by_last_name.setdefault(last_name_norm, [])
        persons_by_last_name[last_name_norm].append(person)

    persons.add(person)


def has_matching_word(phrase1, phrase2):
    for word1 in phrase1.split():
        for word2 in phrase2.split():
            if jaro(word1, word2) > 0.9:
                return True


def find_searchedperson(first_name, last_name, national_id):
    '''
    Try super hard to match this person up. Rather return bad
    match than no match - we can manually search bad matches.
    '''
    if national_id in persons_by_id:
        yield persons_by_id[national_id]

    last_name_norm = normalize_string(last_name)
    matches = []
    if last_name_norm in persons_by_last_name:
        matches = persons_by_last_name[last_name_norm]
    else:
        for key in persons_by_last_name.keys():
            if jaro(last_name_norm, key) > 0.9:
                matches.extend(persons_by_last_name[key])

    first_name_norm = normalize_string(first_name)
    for match in matches:
        key = normalize_string(match.first_name)
        '''
        A match is valid if:
        1. We don't have a first name because only last name
           was used in a failed search.
        2. The entire first name string is similar.
        3. One of the first names are similar.
        '''
        if (not key and isinstance(match, SearchedPersonNotFound)) \
                or jaro(first_name_norm, key) > 0.9 \
                or has_matching_word(first_name_norm, key):
            yield match


class Searcher(windeeds.ResultsScraper):
    resultsdialog_re = re.compile(ur'^\s*ShowCipcListDialog\("(?P<html>.*)"\);\s*$')
    pricingdialog_re = re.compile(ur'^\s*PriceWarningDialog\.Load\("(?P<html>.*)"\);\s*$')

    def __init__(self, limit, name='windeedssearch', config=None):
        super(Searcher, self).__init__(name, config)

        self.limit = limit
        for fn_name in ('search_persons', 'search_director', 'select_directors'):
            task = Task(self, getattr(self, fn_name))
            setattr(self, fn_name, task)

    def init_session(self, csv):
        self.searched_persons = set()
        session = self.Session()
        windeeds.login_session(session)
        # collect existing results before searching
        self.all_results.run(csv, session)
        # run searches for unmatched persons
        self.search_persons.run(csv, session)

    def scrape_result(self, csv, session, data):
        url = data.get('SearchAction')
        if 'Cipc' not in url:
            return
        if 'DirectorResult' in url:
            cls = SearchedPerson
        elif 'NoResult' in url and 'Director' in data.get('SearchType'):
            cls = SearchedPersonNotFound
        else:
            return
        description = data.get('Description').decode('utf8')
        record_searchedperson(cls(
            url=url,
            description=description,
        ))

    def search_persons(self, csv, session):
        search_count = 0
        for data in gdocs_persons():
            matches = list(find_searchedperson(
                data['First Name'],
                data['Last Name'],
                data['ID #']
            ))
            if matches:
                self.log.debug('Skipping %s (matched %r)' %
                               (data['Full Name'], matches))
            else:
                # NOTE: don't run this concurrently
                # Windeeds keeps track of the sequence of form
                # submissions in the session
                self.search_director(csv, session, data)
                search_count += 1
                if search_count >= self.limit:
                    break

    def search_director(self, csv, session, data):
        self.log.debug('Searching %s' % data['Full Name'])
        params = {
            'Surname': data['Last Name'],
            'FirstNames': data['First Name'],
            'IdNumber': data['ID #'].strip(),
            'ReferenceRequired': 'False',
            'Reference': '',
        }
        data = session.post(DIRECTOR_SEARCH_URL, params).json()
        if not data['success'] or data['type'] != 'DATA':
            return
        match = self.resultsdialog_re.match(data['action'])
        if not match:
            return
        doc = html.fromstring(match.group('html').decode('unicode-escape'))
        self.select_directors(csv, session, doc)

    def select_directors(self, csv, session, doc):
        params = {}
        for el in doc.get_element_by_id('CipcListDialogForm') \
                     .findall('./input[@type="hidden"]'):
            params[el.get('name')] = el.get('value')

        selection = []
        count = 0
        el = doc.get_element_by_id('DirectorList')
        el = el.find_class('list-row')[0]  # select first result only
        if 'highlight-level-0' in el.get('class'):
            el = el.find_class('list-subrow')
        else:
            el = el.find_class('highlight-level-1')
        for result in chain.from_iterable([e.findall('.//input[1]') for e in el]):
            selection.append({
                'Description': result.get('data-description'),
                'DbKey': result.get('value'),
            })
            count += int(result.get('directorshipcount'))

        if not selection:
            self.log.debug('Cannot select directors')
            return

        params['DirectorshipCount'] = str(count)
        params['SerializedSelection'] = json.dumps([{'Items': selection}])
        # request results for selected directors
        data = session.post(DIRECTOR_SELECT_URL, params).json()
        if not data['success']:
            self.log.debug('Bad search')
            return
        if data['type'] != 'DIALOG':
            # no confirmation necessary
            return
        match = self.pricingdialog_re.match(data['action'])
        if not match:
            self.log.debug('Cannot confirm search')
            return
        doc = html.fromstring(match.group('html').encode('utf8')
                                                 .decode('string-escape'))
        price_type = doc.get_element_by_id('PriceWarningTypeHidden').get('value')
        params = {
            'PriceWarningType': price_type,
            'DoNotShowAgain': 'false',
            'ListHistoricLoadedSearch': '',
            'FlagKeyReferenceSpecified': '',
        }
        # confirm payment
        res = session.post(DIRECTOR_SELECT_URL, params)
        assert res.status_code == 200


def scrape(limit):
    searcher = Searcher(limit)
    csv = MultiCSV()
    searcher.init_session(csv)
    csv.close()
    searcher.report()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '--limit',
        type=int,
        help="The maximum number of names that will be searched",
        default=10,
        action='store',
        dest='limit'
    )
    limit = parser.parse_args().limit
    scrape(limit)
