#!/usr/bin/env python

# Copyright ICANN
# Original developed by john.bond@icann.org
# Modified for use with Hedgehog by sinodun.com

import sys
import time
import rssac
import socket
import argparse
import datetime
import logging
import StringIO
import SocketServer
import dns.name
import dns.tsig
import dns.query
import dns.message
import dns.tsigkeyring
import psycopg2
import os, pwd, grp
import yaml

class DnsReaderServer(SocketServer.UDPServer):
    '''
    SocketServer.ThreadingUDPServer 

    Instance variables:
    
    - RequestHandlerClass
    '''

    keyname   = None
    keyring   = None
    keyalgorithm = None
    
    def __init__(self, server_address, RequestHandlerClass, server_name, key=None, keyname=None, keyalgo=None):
        SocketServer.UDPServer.__init__(
                self, server_address, RequestHandlerClass)
        
        ''' TODO tell make to define this path/file '''
        config_doc = file('/opt/hedgehog/etc/hedgehog/hedgehog.yaml', 'r')
        config = yaml.load(config_doc)
        config_doc.close()
        db_host = config['database']['host']
        db_port = config['database']['port']
        db_name = config['database']['name']
        db_owner =  config['database']['owner']
        db_password = config['database']['owner_pass']
                
        ''' We are bound to any restricted ports by this stage '''
        ''' Now do everything as a non-root user '''
        ''' Get the uid/gid from the db_owner name '''
        ''' This assumes that db_owner == username to execute as '''
        running_uid = pwd.getpwnam(db_owner).pw_uid
        running_gid = grp.getgrnam(db_owner).gr_gid
        ''' Clear group privileges '''
        os.setgroups([])
        ''' set new uid/gid '''
        os.setgid(running_gid)
        os.setuid(running_uid)
        
        ''' Start logger '''
        self.logger = logging.getLogger('rssac_propagation.server.DnsReaderServer')
        
        if key and keyname:
            self.keyname          = keyname
            self.keyring          = dns.tsigkeyring.from_text({ keyname : key})
        if keyalgo:
            self.keyalgorithm     = dns.name.from_text(keyalgo)
            
        ''' Set up database connection '''
        self.conn = psycopg2.connect(database=db_name, user=db_owner, password=db_password,
                                host=db_host, port=db_port)
        cur = self.conn.cursor()
        ''' TODO: check db api version '''
        
        ''' Get plot id '''
        self.load_time_plot_id = self._get_plot_id('load_time')
        self.zone_size_plot_id = self._get_plot_id('zone_size')

        ''' Get server id '''
        sql = "SELECT id from server where name=%s;"
        data = (server_name, )
        fsql = cur.mogrify(sql, data)
        self.logger.debug('SQL Query: {}'.format(fsql))
        cur.execute(sql, data)
        server_id_tuple = cur.fetchone()
        if server_id_tuple == None:
            cur.close()
            raise ValueError('Server is not defined in the database')
        self.logger.debug('SQL Result: Server {} has id {}'.format(server_name, server_id_tuple[0]))
        self.db_server_id = server_id_tuple[0]
        
        ''' Get the list of nodes to query '''
        self.nodes = {}
        ''' TODO: add ip address to db '''
        sql="SELECT id, '192.168.1.148' as ip FROM node WHERE server_id=%s"
        data = (self.db_server_id, )
        fsql = cur.mogrify(sql, data)
        self.logger.debug('SQL: {}'.format(fsql))
        cur.execute(sql, data)
        for node in cur:
            self.nodes[node[0]] = node[1]
        if not self.nodes:
            cur.close()
            raise ValueError('No nodes found for server id {}'.format(self.db_server_id))
        self.logger.debug('SQL Result: Server id {} has node(s) id {}'.format(self.db_server_id, self.nodes))
    
        ''' Get max serial number '''
        ''' This is read at startup and state is maintained while the server is running '''
        sql = "SELECT max(data.key2) FROM data WHERE plot_id=%s AND server_id=%s"
        data = (self.load_time_plot_id, self.db_server_id)
        fsql = cur.mogrify(sql, data)
        self.logger.debug('SQL: {}'.format(fsql))
        cur.execute(sql, data)
        max_serial_tuple = cur.fetchone()
        if max_serial_tuple[0] == None:
            self.max_serial=0
        else:
            self.max_serial=max_serial_tuple[0]
    
        cur.close()
        
    def _get_plot_id(self, plot_name):
        cur = self.conn.cursor()
        sql = "SELECT id from dataset where name=%s;"
        data = (plot_name, )
        fsql = cur.mogrify(sql, data)
        self.logger.debug('SQL Query: {}'.format(fsql))
        cur.execute(sql, data)
        plot_id_tuple = cur.fetchone()
        if plot_id_tuple == None:
            cur.close()
            raise ValueError('Plot is not defined in the database')
        self.logger.debug('SQL Result: Plot {} has id {}'.format(plot_name, plot_id_tuple[0]))
        cur.close()
        return plot_id_tuple[0]
        
class DnsReaderHandler(SocketServer.BaseRequestHandler):
    '''
    Base Handler class 
    '''

    message  = None
    serial   = None
    data     = None
    socket   = None
    qname    = None

    def __init__(self, request, client_address, server):
        SocketServer.BaseRequestHandler.__init__(self, request, client_address, server)

    def _process_load_time(self):
        start = time.time()
        self.server.logger.debug('SQL Result: Server id {} has max serial {}'.format(self.server.db_server_id, self.server.max_serial))
        zone_check = rssac.CheckZone(self.qname, self.server.nodes,
                self.serial, start)
        zone_check.check_propagation()
        cur = self.server.conn.cursor()
        for node, end in zone_check.nodes_report.items():
            if type(end) is str:
                load_time = end
            else:
                load_time = end - start
            sql = "INSERT INTO data (starttime, server_id, node_id, plot_id, key1, key2, value) VALUES (%s,%s,%s,%s,%s,%s,%s)"
            data = (datetime.datetime.fromtimestamp(start), self.server.db_server_id, node, self.server.load_time_plot_id, self.qname, self.serial, load_time)
            fsql = cur.mogrify(sql, data)
            self.server.logger.debug('SQL: {}'.format(fsql))
            cur.execute(sql, data)
        
        '''' record this serial as the new max(serial) '''
        self.server.max_serial = self.serial
        self.server.conn.commit()
        cur.close()
        
    def _get_zone_size(self):
        zone      = StringIO.StringIO()
        zone_size = 0
        xfr       = dns.query.xfr(self.client_address[0], self.qname, keyname=self.server.keyname,
                    keyring=self.server.keyring, keyalgorithm=self.server.keyalgorithm)
        try:
            for message in xfr:
                for ans in message.answer:
                    ans.to_wire(zone, origin=dns.name.root)
        except dns.exception.FormError:
            self.server.logger.error('Error attempting AXFR from {}'.format(self.client_address[0]))
        else:
            zone_size = sys.getsizeof(zone.getvalue())
        zone.close()
        return zone_size

    def _process_zone_size(self):
        start = time.time()
        zone_size = self._get_zone_size()
        if zone_size == 0:
            ''' Return because we didn't get an answer '''
            return
        cur = self.server.conn.cursor()
        for node in self.server.nodes.keys():
            sql = "INSERT INTO data (starttime, server_id, node_id, plot_id, key1, key2, value) VALUES (%s,%s,%s,%s,%s,%s,%s)"
            data = (datetime.datetime.fromtimestamp(start), self.server.db_server_id, node, self.server.zone_size_plot_id, self.qname, self.serial, zone_size)
            fsql = cur.mogrify(sql, data)
            self.server.logger.debug('SQL: {}'.format(fsql))
            cur.execute(sql, data)
            self.server.conn.commit()
        cur.close()    
                    
    def send_response(self):
        ''' Send notify response '''
        response = dns.message.make_response(self.message)
        self.socket.sendto(response.to_wire(), self.client_address)
        
    def parse_dns(self):
        '''
        parse the data package into dns elements
        '''
        self.data = str(self.request[0]).strip()
        self.socket = self.request[1]
        #incoming Data
        try:
            self.message = dns.message.from_wire(self.data)
        except dns.name.BadLabelType:
            #Error processing lable (bit flip?)
            self.server.logger.error('Received Bad label Type from {}'.format(self.client_address[0]))
        except dns.message.ShortHeader:
            #Received junk
            self.server.logger.error('Received Junk from {}'.format(self.client_address[0]))
        else:
            current_time = int(time.time())
            if self.message.opcode() == 4:
                self.qname = self.message.question[0].name.to_text()
                if len(self.message.answer) > 0:
                    answer = self.message.answer[0]
                    self.serial = answer.to_rdataset()[0].serial
                    
                    if int(self.serial) <= int(self.server.max_serial):
                        self.server.logger.debug('{}:{}:load-time already processed or lower then max({})'.format(
                            self.qname, self.serial, self.server.max_serial))
                        self.send_response()
                        return False
                    else:    
                        self.server.logger.debug('Received notify for {} from {}'.format(self.serial, self.client_address[0]))
                        self.send_response()
                        return True
                else:
                    self.server.logger.error('Received notify with no serial from {}'.format(self.client_address[0]))
                    self.send_response()
        return False

    def handle(self):
        '''
        RequestHandlerClass handle function
        handler listens for dns packets
        '''
        if self.parse_dns():
            self._process_load_time()
            self._process_zone_size()
    
def main():
    ''' parse cmd line args '''
    parser = argparse.ArgumentParser(description='nofify receiver')
    parser.add_argument('--tsig-name')
    parser.add_argument('--tsig-key')
    parser.add_argument('--tsig-algo', 
            choices=['hmac-sha1', 'hmac-sha224', 'hmac-sha256', 'hmac-sha384','hmac-sha512'])
    parser.add_argument('-v', '--verbose', action='count', help='Increase verbosity')
    parser.add_argument('--log', default='server.log')
    parser.add_argument('-l', '--listen', metavar="0.0.0.0:53", 
            default="0.0.0.0:53", help='listen on address:port ')
    parser.add_argument('-s','--server', help='Server name', required=True)
    args = parser.parse_args()
    host, port = args.listen.split(":")
    
    ''' configure logging '''
    log_level = logging.ERROR
    log_format ='%(asctime)s:%(levelname)s:%(name)s:%(funcName)s(%(levelno)s):%(message)s'
    if args.verbose == 1:
        log_level = logging.WARN
    elif args.verbose == 2:
        log_level = logging.INFO
    elif args.verbose > 2:
        log_level = logging.DEBUG
    logging.basicConfig(level=log_level, filename=args.log, format=log_format)
    logger = logging.getLogger('rssac_propagation.server')
    logger.info('get_notify starting up...')
    
    ''' Init and then run the server '''
    server = DnsReaderServer((host, int(port)), DnsReaderHandler, args.server,
            args.tsig_key, args.tsig_name, args.tsig_algo) 
    server.serve_forever()
    
if __name__ == "__main__":
    main()