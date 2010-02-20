 # This Python file uses the following encoding: utf-8
import os,sys,traceback
from django.core.management.base import NoArgsCommand
from optparse import make_option
from django.conf import settings
from django.contrib.contenttypes.models import ContentType

import urllib2,urllib
import re
import gzip
import simplejson
import datetime
import time
import logging

from knesset.mks.models import *
from knesset.laws.models import *
from knesset.links.models import *
from django.db import connection
from django.db.models import Max,Count

import mk_info_html_parser as mk_parser

ENCODING = 'utf8'

DATA_ROOT = getattr(settings, 'DATA_ROOT',
                    os.path.join(settings.PROJECT_ROOT, 'data'))

logger = logging.getLogger("open-knesset.syncdata")

class Command(NoArgsCommand):
    option_list = NoArgsCommand.option_list + (
        make_option('--all', action='store_true', dest='all',
            help="runs all the syncdata sub processes (like --download --load --process --dump)"),
        make_option('--download', action='store_true', dest='download',
            help="download data from knesset website to local files."),
        make_option('--load', action='store_true', dest='load',
            help="load the data from local files to the db."),
        make_option('--process', action='store_true', dest='process',
            help="run post loading process."),
        make_option('--dump', action='store_true', dest='dump-to-file',
            help="write votes to tsv files (mainly for debug, or for research team)."),
        make_option('--update', action='store_true', dest='update',
            help="online update of votes data."),
        
    )
    help = "Downloads data from sources, parses it and loads it to the Django DB."

    requires_model_validation = False

    last_downloaded_vote_id = 0
    last_downloaded_member_id = 0

    def read_laws_page(self,index):
        url = 'http://www.knesset.gov.il/privatelaw/plaw_display.asp?LawTp=2'
        data = urllib.urlencode({'RowStart':index})
        urlData = urllib2.urlopen(url,data)
        page = urlData.read().decode('windows-1255').encode('utf-8')
        return page

    def parse_laws_page(self,page):
        names = []
        exps = []
        links = []
        count = -1
        lines = page.split('\n')
        for line in lines:
            #print line
            r = re.search("""Href=\"(.*?)\">""",line)
            if r != None:
                link = 'http://www.knesset.gov.il/privatelaw/' + r.group(1)
            r = re.search("""<td class="LawText1">(.*)""",line)
            if r != None:
                name = r.group(1).replace("</td>","").strip()
                if len(name)>1 and name.find('span')<0:
                    names.append(name)
                    links.append(link)
                    exps.append('')
                    count += 1
            if re.search("""arrResume\[\d*\]""",line) != None:
                r = re.search("""\"(.*)\"""",line)
                if r != None:
                    try:
                        exps[count] += r.group(1).replace('\t',' ')
                    except: 
                        pass

        return (names,exps,links)

    def get_laws_data(self):
        f = gzip.open(os.path.join(DATA_ROOT, 'laws.tsv.gz'), "wb")
        for x in range(0,910,26): # TODO: find limits of download
        #for x in range(0,50,26): # for debug
            page = self.read_laws_page(x)
            (names,exps,links) = self.parse_laws_page(page)
            for (name,exp,link) in zip(names,exps,links):
                f.write("%s\t%s\t%s\n" % (name,exp,link))
        f.close()

    def update_votes(self):
        """This function updates votes data online, without saving to files."""        
        current_max_src_id = Vote.objects.aggregate(Max('src_id'))['src_id__max']
        if current_max_src_id == None: # the db contains no votes, meaning its empty
            print "DB is empty. --update can only be used to update, not for first time loading. \ntry --all, or get some data using initial_data.json\n"
            return
        vote_id = current_max_src_id+1 # first vote to look for is the max_src_id we have plus 1
        limit_src_id = vote_id + 20 # look for next 20 votes. if results are found, this value will be incremented. 
        while vote_id < limit_src_id:        
            (page, vote_src_url) = self.read_votes_page(vote_id)
            title = self.get_page_title(page)
            if(title == """הצבעות במליאה-חיפוש"""): # found no vote with this id                
                logger.debug("no vote found at id %d" % vote_id)
            else:
                limit_src_id = vote_id + 20 # results found, so we'll look for at least 20 more votes
                (vote_label, vote_meeting_num, vote_num, date) = self.get_vote_data(page)
                
        #(vote_id, vote_src_url, vote_label, vote_meeting_num, vote_num, vote_time_string, count_for, count_against, count_abstain, count_no_vote) = line.split('\t') 
        #f2.write("%d\t%s\t%s\t%s\t%s\t%s\t%d\t%d\t%d\t%d\n" % (id, src_url, name, meeting_num, vote_num, date))
                logger.debug("downloaded data with vote id %d" % vote_id)
                vote_time_string = date.replace('&nbsp;',' ')
                for i in self.heb_months:
                    if i in vote_time_string:
                        month = self.heb_months.index(i)+1
                day = re.search("""(\d\d?)""", vote_time_string).group(1)
                year = re.search("""(\d\d\d\d)""", vote_time_string).group(1)
                vote_hm = datetime.datetime.strptime ( vote_time_string.split(' ')[-1], "%H:%M" )
                vote_date = datetime.date(int(year),int(month),int(day))
                vote_time = datetime.datetime(int(year), int(month), int(day), vote_hm.hour, vote_hm.minute)
                #vote_label_for_search = self.get_search_string(vote_label)

                try:
                    v = Vote.objects.get(src_id=vote_id)                
                    created = False
                except:
                    v = Vote(title=vote_label, time_string=vote_time_string, importance=1, src_id=vote_id, time=vote_time)
                    try:
                        vote_meeting_num = int(vote_meeting_num)
                        v.meeting_number = vote_meeting_num
                    except:
                        pass
                    try:
                        vote_num = int(vote_num)
                        v.vote_number = vote_num
                    except:
                        pass
                    v.src_url = vote_src_url
                    v.save()
                    if v.full_text_url != None:
                        l = Link(title=u'מסמך הצעת החוק באתר הכנסת', url=v.full_text_url, content_type=ContentType.objects.get_for_model(v), object_pk=v.id)
                        l.save()

                results = self.read_member_votes(page)
                for (voter,voter_party,vote) in results:
                    #f.write("%d\t%s\t%s\t%s\n" % (id,voter,party,vote))                    

                    # transform party names to canonical form
                    if(voter_party in self.party_aliases):
                        voter_party = self.party_aliases[voter_party]

                    p = Party.objects.get(name=voter_party)
                                    
                    # get the member voting                
                    try:
                        m = Member.objects.get(name=voter)
                    except:   # if there are several people with same name, 
                        m = Member.objects.filter(name=voter).order_by('-date_of_birth')[0] # choose the younger. TODO: fix this
                        
                    # add the current member's vote
                    va,created = VoteAction.objects.get_or_create(vote = v, member = m, type = vote)
                    if created:
                        va.save()
            vote_id += 1 

    def get_votes_data(self):
        self.update_last_downloaded_vote_id()
        f  = gzip.open(os.path.join(DATA_ROOT, 'results.tsv.gz'), "ab")
        f2 = gzip.open(os.path.join(DATA_ROOT, 'votes.tsv.gz'),"ab")
        r = range(self.last_downloaded_vote_id+1,13400) # this is the range of page ids to go over. currently its set manually.
        for id in r:
            (page, src_url) = self.read_votes_page(id)
            title = self.get_page_title(page)
            if(title == """הצבעות במליאה-חיפוש"""): # found no vote with this id
                logger.debug("no vote found at id %d" % id)
            else:
                count_for = 0
                count_against = 0
                count_abstain = 0
                count_no_vote = 0
                (name, meeting_num, vote_num, date) = self.get_vote_data(page)
                results = self.read_member_votes(page)
                for (voter,party,vote) in results:
                    f.write("%d\t%s\t%s\t%s\n" % (id,voter,party,vote))
                    if(vote=="for"):
                        count_for += 1
                    if(vote=="against"):
                        count_against += 1
                    if(vote=="abstain"):
                        count_abstain += 1
                    if(vote=="no-vote"):
                        count_no_vote += 1
                f2.write("%d\t%s\t%s\t%s\t%s\t%s\t%d\t%d\t%d\t%d\n" % (id, src_url, name, meeting_num, vote_num, date, count_for, count_against, count_abstain, count_no_vote))
                logger.debug("downloaded data with vote id %d" % id)
            #print " %.2f%% done" % ( (100.0*(float(id)-r[0]))/(r[-1]-r[0]) )
        f.close()
        f2.close()        

    def update_last_downloaded_member_id(self):
        """
        Reads local members file, and sets self.last_downloaded_member_id to the highest id found in the file.
        This is later used to skip downloading of data alreay downloaded.
        """
        try:
            f = gzip.open(os.path.join(DATA_ROOT, 'members.tsv.gz'))
        except:
            self.last_downloaded_member_id = 0
            logger.debug("members file does not exist. setting last_downloaded_member_id to 0")
            return            
        content = f.read().split('\n')
        for line in content:
            if(len(line)<2):
                continue
            s = line.split('\t')
            id = int(s[0])
            if id > self.last_downloaded_member_id:
                self.last_downloaded_member_id = id
        logger.debug("last member id found in local files is %d. " % self.last_downloaded_member_id)
        f.close()
    
    def get_members_data(self):
        """downloads members data to local files
        """
        self.update_last_downloaded_member_id()

        f  = gzip.open(os.path.join(DATA_ROOT, 'members.tsv.gz'), "ab")

        fields = ['img_link','טלפון','פקס','אתר נוסף','דואר אלקטרוני','מצב משפחתי','מספר ילדים','תאריך לידה','שנת לידה','מקום לידה','תאריך פטירה','שנת עלייה'] 
        # note that hebrew strings order is right-to-left
        # so output file order is id, name, img_link, phone, ...

        fields = [unicode(field.decode('utf8')) for field in fields]

        for id in range(self.last_downloaded_member_id+1,900): # TODO - find max member id in knesset website and use here
            m = mk_parser.MKHtmlParser(id).Dict
            if (m.has_key('name') and m['name'] != None): name = m['name'].encode(ENCODING).replace('&nbsp;',' ').replace(u'\xa0',' ')
            else: continue
            f.write("%d\t%s\t" % (  id, name ))
            for field in fields:
                value = ''
                if (m.has_key(field) and m[field]!=None): 
                    value = m[field].encode(ENCODING)
                f.write("%s\t" % (  value ))
            f.write("\n")
            
        f.close()

    def download_all(self):
        self.get_members_data()        
        self.get_votes_data()
        self.get_laws_data()


    def update_last_downloaded_vote_id(self):
        """
        Reads local votes file, and sets self.last_downloaded_id to the highest id found in the file.
        This is later used to skip downloading of data alreay downloaded.
        """
        try:
            f = gzip.open(os.path.join(DATA_ROOT, 'votes.tsv.gz'))
        except:
            self.last_downloaded_vote_id = 0
            logger.debug("votes file does not exist. setting last_downloaded_vote_id to 0")
            return            
        content = f.read().split('\n')
        for line in content:
            if(len(line)<2):
                continue
            s = line.split('\t')
            vote_id = int(s[0])
            if vote_id > self.last_downloaded_vote_id:
                self.last_downloaded_vote_id = vote_id
        logger.debug("last id found in local files is %d. " % self.last_downloaded_vote_id)
        f.close()


    def update_members_from_file(self):
        f = gzip.open(os.path.join(DATA_ROOT, 'members.tsv.gz'))
        content = f.read().split('\n')
        for line in content:
            if len(line) <= 1:
                continue
            (member_id, name, img_url, phone, fax, website, email, family_status, number_of_children, 
             date_of_birth, year_of_birth, place_of_birth, date_of_death, year_of_aliyah, extra) = line.split('\t') 
            if email != '':
                email = email.split(':')[1]
            try:
                if date_of_birth.find(',')>=0:
                    date_of_birth = date_of_birth.split(',')[1].strip(' ')
                date_of_birth = datetime.datetime.strptime ( date_of_birth, "%d/%m/%Y" )
            except:
                date_of_birth = None
            try:
                if date_of_birth.find(',')>=0:
                    date_of_death = date_of_birth.split(',')[1].strip(' ')
                date_of_death = datetime.datetime.strptime ( date_of_death, "%d/%m/%Y" )
            except:
                date_of_death = None
            try: year_of_birth = int(year_of_birth)
            except: year_of_birth = None
            try: year_of_aliyah = int(year_of_aliyah)
            except: year_of_aliyah = None
            try: number_of_children = int(number_of_children)
            except: number_of_children = None

            try:
                m = Member.objects.get(id=member_id)
            except: # member_id not found. create new
                m = Member(id=member_id, name=name, img_url=img_url, phone=phone, fax=fax, website=None, email=email, family_status=family_status, 
                            number_of_children=number_of_children, date_of_birth=date_of_birth, place_of_birth=place_of_birth, 
                            date_of_death=date_of_death, year_of_aliyah=year_of_aliyah)
                m.save()
                if len(website)>0:
                    l = Link(title='אתר האינטרנט של %s' % name, url=website, content_type=ContentType.objects.get_for_model(m), object_pk=m.id)
                    l.save()


    def get_search_string(self,s):
        s = s.replace('\xe2\x80\x9d','').replace('\xe2\x80\x93','')
        return re.sub(r'["\(\) ,-]', '', s)

    heb_months = ['ינואר','פברואר','מרץ','אפריל','מאי','יוני','יולי','אוגוסט','ספטמבר','אוקטובר','נובמבר','דצמבר']


    # some party names appear in the knesset website in several forms.
    # this dictionary is used to transform them to canonical form.
    party_aliases = {'עבודה':'העבודה',
                    'ליכוד':'הליכוד',
                    'ש"ס-התאחדות ספרדים שומרי תורה':'ש"ס',
                    'יחד (ישראל חברתית דמוקרטית) והבחירה הדמוקרטית':'יחד (ישראל חברתית דמוקרטית) והבחירה הדמוקרטית',
                    'בל"ד-ברית לאומית דמוקרטית':'בל"ד',
                    'אחריות לאומית':'קדימה',
                    'יחד (ישראל חברתית דמוקרטית) והבחירה הדמוקרטית':'מרצ-יחד והבחירה הדמוקרטית',
                    'יחד והבחירה הדמוקרטית':'מרצ-יחד והבחירה הדמוקרטית',
                    'יחד והבחירה הדמוקרטית (מרצ לשעבר)':'מרצ-יחד והבחירה הדמוקרטית',
                    'יחד (ישראל חברתית דמוקרטית) והבחירה הדמוקרטית':'מרצ-יחד והבחירה הדמוקרטית',
                    'יחד  (ישראל חברתית דמוקרטית) והבחירה הדמוקרטית':'מרצ-יחד והבחירה הדמוקרטית',
                    }


    def update_db_from_files(self):
        logger.debug("Update DB From Files")

        try:
            laws = [] # of lists: [name,name_for_search,explanation,link]
            f = gzip.open(os.path.join(DATA_ROOT, 'laws.tsv.gz'))
            content = f.read().split('\n')
            for line in content:
                law = line.split('\t')
                if len(law)==3:
                    name_for_search = self.get_search_string(law[0])
                    law.insert(1, name_for_search)
                    laws.append(law)
            f.close()

            parties = dict() # key: party-name; value: Party
            members = dict() # key: member-name; value: Member
            votes   = dict() # key: id; value: Vote
            memberships = dict() # key: (member.id,party.id)
            current_vote = None # used to track what vote we are on, to create vote objects only for new votes
            current_max_src_id = Vote.objects.aggregate(Max('src_id'))['src_id__max']
            if current_max_src_id == None: # the db contains no votes, meanins its empty
                current_max_src_id = 0

            logger.debug("processing votes data")
            f = gzip.open(os.path.join(DATA_ROOT, 'votes.tsv.gz'))
            content = f.read().split('\n')
            for line in content:
                if len(line) <= 1:
                    continue
                (vote_id, vote_src_url, vote_label, vote_meeting_num, vote_num, vote_time_string, count_for, count_against, count_abstain, count_no_vote) = line.split('\t') 
                #if vote_id < current_max_src_id: # skip votes already parsed.
                #    continue  
                vote_time_string = vote_time_string.replace('&nbsp;',' ')
                for i in self.heb_months:
                    if i in vote_time_string:
                        month = self.heb_months.index(i)+1
                day = re.search("""(\d\d?)""", vote_time_string).group(1)
                year = re.search("""(\d\d\d\d)""", vote_time_string).group(1)
                vote_hm = datetime.datetime.strptime ( vote_time_string.split(' ')[-1], "%H:%M" )
                vote_date = datetime.date(int(year),int(month),int(day))
                vote_time = datetime.datetime(int(year), int(month), int(day), vote_hm.hour, vote_hm.minute)
                vote_label_for_search = self.get_search_string(vote_label)

                if vote_date < datetime.date(2009, 02, 24): # vote before 18th knesset
                    continue

                try:
                    v = Vote.objects.get(src_id=vote_id)                
                    created = False
                except:
                    v = Vote(title=vote_label, time_string=vote_time_string, importance=1, src_id=vote_id, time=vote_time)
                    try:
                        vote_meeting_num = int(vote_meeting_num)
                        v.meeting_number = vote_meeting_num
                    except:
                        pass
                    try:
                        vote_num = int(vote_num)
                        v.vote_number = vote_num
                    except:
                        pass
                    v.src_url = vote_src_url
                    for law in laws:
                        (law_name,law_name_for_search,law_exp,law_link) = law
                        if vote_label_for_search.find(law_name_for_search) >= 0:
                            v.summary = law_exp
                            v.full_text_url = law_link                    
                    v.save()
                    if v.full_text_url != None:
                        l = Link(title=u'מסמך הצעת החוק באתר הכנסת', url=v.full_text_url, content_type=ContentType.objects.get_for_model(v), object_pk=v.id)
                        l.save()
                votes[int(vote_id)] = v
            f.close()

            logger.debug("processing member votes data")
            f = gzip.open(os.path.join(DATA_ROOT, 'results.tsv.gz'))
            content = f.read().split('\n')
            for line in content:
                if(len(line)<2):
                    continue
                s = line.split('\t') # (id,voter,party,vote)
                
                vote_id = int(s[0])
                voter = s[1]
                voter_party = s[2]

                # transform party names to canonical form
                if(voter_party in self.party_aliases):
                    voter_party = self.party_aliases[voter_party]

                vote = s[3]

                try:
                    v = votes[vote_id]
                except KeyError: #this vote was skipped in this read, also skip voteactions and members
                    continue 
                vote_date = v.time.date()

                # create/get the party appearing in this vote 
                if voter_party in parties:
                    p = parties[voter_party]
                    created = False
                else:
                    p,created = Party.objects.get_or_create(name=voter_party)
                    parties[voter_party] = p
                #if created: # this is magic needed because of unicode chars. if you don't do this, the object p will have gibrish as its name. 
                            #only when it comes back from the db it has valid unicode chars.
                #    p = Party.objects.get(name=voter_party) 
                
                # use this vote's time to update the party's start date and end date
                if (p.start_date is None) or (p.start_date > vote_date):
                    p.start_date = vote_date
                if (p.end_date is None) or (p.end_date < vote_date):
                    p.end_date = vote_date
                if created: # save on first time, so it would have an id, be able to link, etc. all other updates are saved in the end
                    p.save() 
                
                # create/get the member voting
                if voter in members:
                    m = members[voter]
                    created = False
                else:
                    try:
                        m = Member.objects.get(name=voter)
                    except:   # if there are several people with same age, 
                        m = Member.objects.filter(name=voter).order_by('-date_of_birth')[0] # choose the younger. TODO: fix this
                    members[voter] = m
                #m.party = p;
                #if created: # again, unicode magic
                #    m = Member.objects.get(name=voter)
                # use this vote's date to update the member's dates.
                if (m.start_date is None) or (m.start_date > vote_date):
                    m.start_date = vote_date
                if (m.end_date is None) or (m.end_date < vote_date):
                    m.end_date = vote_date
                #if created: # save on first time, so it would have an id, be able to link, etc. all other updates are saved in the end
                #    m.save()
        
                    
                # create/get the membership (connection between member and party)
                if ((m.id,p.id) in memberships):
                    ms = memberships[(m.id,p.id)]                
                    created = False
                else:
                    ms,created = Membership.objects.get_or_create(member=m,party=p)
                    memberships[(m.id,p.id)] = ms
                #if created: # again, unicode magic
                #    ms = Membership.objects.get(member=m,party=p)
                # again, update the dates on the membership
                if (ms.start_date is None) or (ms.start_date > vote_date):
                    ms.start_date = vote_date
                if (ms.end_date is None) or (ms.end_date < vote_date):
                    ms.end_date = vote_date
                if created: # save on first time, so it would have an id, be able to link, etc. all other updates are saved in the end
                    ms.save()    
                    
                # add the current member's vote
                
                va,created = VoteAction.objects.get_or_create(vote = v, member = m, type = vote)
                if created:
                    va.save()
                
            logger.debug("done")
            logger.debug("saving data: %d parties, %d members, %d memberships " % (len(parties), len(members), len(memberships) ))
            for p in parties:
                parties[p].save()
            for m in members:
                members[m].save()
            Member.objects.filter(end_date__isnull=True).delete() # remove members that haven't voted at all - no end date
            for ms in memberships:
                memberships[ms].save()
            #for va in voteactions:
            #    voteactions[va].save()
            logger.debug("done")
            f.close()
        except:
            exceptionType, exceptionValue, exceptionTraceback = sys.exc_info()
            logger.error("%s", ''.join(traceback.format_exception(exceptionType, exceptionValue, exceptionTraceback)))
            

    def calculate_votes_importances(self):
        """
        Calculates votes importances. currently uses rule of thumb: number of voters against + number of voters for / 120.
        """    
        for v in Vote.objects.all():
            v.importance = float(v.votes.filter(voteaction__type='for').count() + v.votes.filter(voteaction__type='against').count()) / 120
            v.save()
        
    def calculate_correlations(self):
        """
        Calculates member pairs correlation on votes.
        """
        # TODO: refactor the hell out of this one...
        return 
        print "Calculate Correlations"
        try:     
            cursor = connection.cursor()
            print "%s truncate correlations table" % str(datetime.datetime.now())
            Correlation.objects.all().delete()
            print "%s calculate correlations"  % str(datetime.datetime.now())
            cursor.execute("""insert into mks_correlation (m1_id,m2_id,score) (
                select m1 as m1_id, m2 as m2_id,sum(score) as score from (
                select a1.member_id as m1,a2.member_id as m2, count(*) as score from (
                (select * from laws_votes where type='for') a1,
                (select * from laws_votes where type='for') a2
                ) where a1.vote_id = a2.vote_id and a1.member_id < a2.member_id group by a1.member_id,a2.member_id
                union
                select a1.member_id as m1,a2.member_id as m2, count(*) as score from (
                (select * from laws_votes where type='against') a1,
                (select * from laws_votes where type='against') a2
                ) where a1.vote_id = a2.vote_id and a1.member_id < a2.member_id group by a1.member_id,a2.member_id
                union
                select a1.member_id as m1,a2.member_id as m2, -count(*) as score from (
                (select * from laws_votes where type='for') a1,
                (select * from laws_votes where type='against') a2
                ) where a1.vote_id = a2.vote_id and a1.member_id < a2.member_id group by a1.member_id,a2.member_id
                union
                select a1.member_id as m1,a2.member_id as m2, -count(*) as score from (
                (select * from laws_votes where type='against') a1,
                (select * from laws_votes where type='for') a2
                ) where a1.vote_id = a2.vote_id and a1.member_id < a2.member_id group by a1.member_id,a2.member_id
                
                ) a group by m1,m2
                )""".replace('\n',''))
            print "%s done"  % str(datetime.datetime.now())
            print "%s normalizing correlation"  % str(datetime.datetime.now())
            cursor.execute("""update mks_correlation,
                (select member_id,sum(vote_count) as vote_count from (
                select member_id,count(*) as vote_count from laws_votes where type='for' group by member_id
                union
                select member_id,count(*) as vote_count from laws_votes where type='against' group by member_id
                ) a 
                group by member_id) a1,
                (select member_id,sum(vote_count) as vote_count from (
                select member_id,count(*) as vote_count from laws_votes where type='for' group by member_id
                union
                select member_id,count(*) as vote_count from laws_votes where type='against' group by member_id
                ) a 
                group by member_id) a2
                set mks_correlation.normalized_score = mks_correlation.score / sqrt(a1.vote_count) / sqrt(a2.vote_count)*100 
                where mks_correlation.m1_id = a1.member_id and mks_correlation.m2_id = a2.member_id""")

        except Exception,e:
            print "error: %s" % e
     

    def read_votes_page(self,voteId, retry=0):
        """
        Gets a votes page from the knesset website.
        returns a string (utf encoded)
        """
        url = "http://www.knesset.gov.il/vote/heb/Vote_Res_Map.asp?vote_id_t=%d" % voteId
        try:        
            urlData = urllib2.urlopen(url)
            page = urlData.read().decode('windows-1255').encode('utf-8')
            time.sleep(2)
        except Exception,e:
            logger.warn(e)
            if retry < 5:
                logger.warn("waiting some time and trying again... (# of retries = %d)" % retry+1)
                page = read_votes_page(self,voteId, retry+1)
            else:
                logger.error("failed too many times. last error: %s", e)
                return None
        return (page, url)

    def read_member_votes(self,page):
        """
        Returns a tuple of (name, party, vote) describing the vote found in page, where:
         name is a member name
         party is the member's party
         vote is 'for','against','abstain' or 'no-vote'
        """
        results = []
        pattern = re.compile("""Vote_Bord""")
        match = pattern.split(page)
        for i in match:
            vote = ""
            if(re.match("""_R1""", i)):
                vote = "for"
            if(re.match("""_R2""", i)):
                vote = "against"
            if(re.match("""_R3""", i)):
                vote = "abstain"
            if(re.match("""_R4""", i)):
                vote = "no-vote"
            if(vote != ""):
                name = re.search("""DataText4>([^<]*)</a>""",i).group(1);
                name = re.sub("""&nbsp;""", " ", name)
                party = re.search("""DataText4>([^<]*)</td>""",i).group(1);
                party = re.sub("""&nbsp;""", " ", party)
                if(party == """ " """):
                    party = last_party
                else:
                    last_party = party 
                results.append((name, party, vote))  
        return results


    def get_page_title(self,page):
        """
        Returns the title of a vote page
        """
        title = re.search("""<TITLE>([^<]*)</TITLE>""", page)
        return title.group(1)

    def get_vote_data(self,page):
        """
        Returns name, meeting number, vote number and date from a vote page
        """
        meeting_num = re.search("""מספר ישיבה: </td>[^<]*<[^>]*>([^<]*)<""", page).group(1)
        vote_num = re.search("""מספר הצבעה: </td>[^<]*<[^>]*>([^<]*)<""", page).group(1)
        name = re.search("""שם החוק: </td>[^<]*<[^>]*>([^<]*)<""", page).group(1)
        name = name.replace("\t"," ")
        name = name.replace("\n"," ")
        name = name.replace("\r"," ")
        name = name.replace("&nbsp;"," ")
        date = re.search("""תאריך: </td>[^<]*<[^>]*>([^<]*)<""",page).group(1)
        return (name, meeting_num, vote_num, date)

    def find_pdf(self,vote_title):
        """
        Gets a vote title, and searches google for relevant files about it.
        NOT FINISHED, AND CURRENTLY NOT USED.
        """
        r = ''
        try:        
            q = vote_title.replace('-',' ').replace('"','').replace("'","").replace("`","").replace(",","").replace('(',' ').replace(')',' ').replace('  ',' ').replace(' ','+')
            q = q.replace('אישור החוק','')
            q = q+'+pdf'
            q = urllib2.quote(q)
            url = "http://ajax.googleapis.com/ajax/services/search/web?v=1.0&q=%s" % q
            r = simplejson.loads(urllib2.urlopen(url).read().decode('utf-8'))['responseData']['results']
        except Exception,e:
            print "error: %s" % e
            return ''
        if len(r) > 0:
            print "full_text_url for vote %s : %s" % (q,r[0]['url'].encode('utf-8'))
            return r[0]['url']
        else:
            #print "didn't find a full_text_url for vote %s , q = %s" % (vote_title,q)
            return ''

    def dump_to_file(self):
        f = open('votes.tsv','wt')
        for v in Vote.objects.filter(time__gte=datetime.date(2009,2,24)):
            if (v.full_text_url != None):
                link = v.full_text_url.encode('utf-8')
            else:
                link = ''            
            if (v.summary != None):
                summary = v.summary.encode('utf-8')
            else:
                summary = ''            
            #for_ids = ",".join([str(m.id) for m in v.votes.filter(voteaction__type='for').all()])
            #against_ids = ",".join([str(m.id) for m in v.votes.filter(voteaction__type='against').all()])
            #f.write("%d\t%s\t%s\t%s\t%s\t%s\t%s\n" % (v.id,v.title.encode('utf-8'),v.time_string.encode('utf-8'),summary, link, for_ids, against_ids))
            f.write("%d\t%s\t%s\t%s\t%s\n" % (v.id, str(v.time), v.title.encode('utf-8'), summary, link))
        f.close()

        f = open('votings.tsv','wt')
        for v in Vote.objects.filter(time__gte=datetime.date(2009,2,24)):
            for va in v.voteaction_set.all():
                f.write("%d\t%d\t%s\n" % (v.id, va.member.id, va.type))
        f.close()

        f = open('members.tsv','wt')
        for m in Member.objects.filter(end_date__gte=datetime.date(2009,2,24)):
            f.write("%d\t%s\t%s\n" % (m.id, m.name.encode('utf-8'), m.Party().__unicode__().encode('utf-8')))
        f.close()


    def handle_noargs(self, **options):
    
        all_options = options.get('all', False)
        download = options.get('download', False)
        load = options.get('load', False)
        process = options.get('process', False)
        dump_to_file = options.get('dump-to-file', False)
        update = options.get('update', False)
        if all_options:
            download = True
            load = True
            process = True
            dump_to_file = True

        if (all([not(all_options),not(download),not(load),not(process),not(dump_to_file),not(update)])):
            print "no arguments found. doing nothing. \ntry -h for help.\n--all to run the full syncdata flow.\n--update for an online dynamic update."

        if download:
            print "beginning download phase"
            self.download_all()    
            #self.get_laws_data()
        
        if load:
            print "beginning load phase"
            self.update_members_from_file()
            self.update_db_from_files()

        if process:
            print "beginning process phase"
            self.calculate_votes_importances()
            #self.calculate_correlations()

        if dump_to_file:
            print "writing votes to tsv file"
            self.dump_to_file()

        if update:
            self.update_votes()

def update_vote_properties(v):
    party_id_member_count_coalition = Party.objects.annotate(member_count=Count('members')).values_list('id','member_count','is_coalition')
    party_ids = [x[0] for x in party_id_member_count_coalition]
    party_member_count = [x[1] for x in party_id_member_count_coalition]
    party_is_coalition = dict(zip(party_ids, [x[2] for x in party_id_member_count_coalition] ))

    for_party_ids = [va.Party().id for va in v.for_votes()]    
    party_for_votes = [sum([x==id for x in for_party_ids]) for id in party_ids]    

    against_party_ids = [va.Party().id for va in v.against_votes()]
    party_against_votes = [sum([x==id for x in against_party_ids]) for id in party_ids]

    party_stands_for = [float(fv)>0.66*(fv+av) for (fv,av) in zip(party_for_votes, party_against_votes)]
    party_stands_against = [float(av)>0.66*(fv+av) for (fv,av) in zip(party_for_votes, party_against_votes)]
    
    party_stands_for = dict(zip(party_ids, party_stands_for))
    party_stands_against = dict(zip(party_ids, party_stands_against))

    coalition_for_votes = sum([x for (x,y) in zip(party_for_votes,party_ids) if party_is_coalition[y]])
    coalition_against_votes = sum([x for (x,y) in zip(party_against_votes,party_ids) if party_is_coalition[y]])
    opposition_for_votes = sum([x for (x,y) in zip(party_for_votes,party_ids) if not party_is_coalition[y]])
    opposition_against_votes = sum([x for (x,y) in zip(party_against_votes,party_ids) if not party_is_coalition[y]])

    coalition_stands_for = (float(coalition_for_votes)>0.66*(coalition_for_votes+coalition_against_votes)) 
    coalition_stands_against = float(coalition_against_votes)>0.66*(coalition_for_votes+coalition_against_votes)
    opposition_stands_for = float(opposition_for_votes)>0.66*(opposition_for_votes+opposition_against_votes)
    opposition_stands_against = float(opposition_against_votes)>0.66*(opposition_for_votes+opposition_against_votes)

    for va in VoteAction.objects.filter(vote=v):
        dirt = False
        if party_stands_for[va.member.Party().id] and va.type=='against':
            va.against_party = True
            dirt = True
        if party_stands_against[va.member.Party().id] and va.type=='for':
            va.against_party = True
            dirt = True
        if va.member.Party().is_coalition:
            if (coalition_stands_for and va.type=='against') or (coalition_stands_against and va.type=='for'):
                va.against_coalition = True
                dirt = True
        else:
            if (opposition_stands_for and va.type=='against') or (opposition_stands_against and va.type=='for'):
                va.against_opposition = True
                dirt = True
        if dirt:
            va.save()


