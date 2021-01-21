# Copyright 2020 A10 Networks, Inc.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

_translators = i18n.TranslatorFactory(domain='a10_migration')

# The primary translation function using the well-known name "_"
_ = _translators.primary

CONF = cfg.CONF

cli_opts = [
    cfg.BoolOpt('all', default=False,
                help='Migrate all load balancers'),
    cfg.StrOpt('lb_id',
               help='Load balancer ID to migrate'),
    cfg.StrOpt('project_id',
               help='Migrate all load balancers owned by this project'),
]

migration_opts = [
    cfg.BoolOpt('delete_after_migration', default=True,
                help='Delete the load balancer records from neutron-lbaas'
                     ' after migration'),
    cfg.BoolOpt('trial_run', default=False,
                help='Run without making changes.'),
    cfg.StrOpt('octavia_account_id', required=True,
               help='The keystone account ID Octavia is running under.'),
    cfg.StrOpt('neutron_db_connection',
               required=True,
               help='The neutron database connection string'),
    cfg.StrOpt('octavia_db_connection',
               required=True,
               help='The octavia database connection string'),
    cfg.StrOpt('a10_nlbaas_db_connection',
               required=False,
               help='The a10 nlbaas database connection string'),
    cfg.StrOpt('a10_oct_connection',
               required=False,
               help='The a10 octavia database connection string'),
    cfg.StrOpt('a10_config_path',
               required=True,
               help='Path to config.py file used by the A10 networks lbaas driver'),
]

cfg.CONF.register_cli_opts(cli_opts)
cfg.CONF.register_opts(migration_opts, group='migration')


def main():
    if len(sys.argv) == 1:
        print('Error: Config file must be specified.')
        print('a10_nlbaas2oct --config-file <filename>')
        return 1
    logging.register_options(cfg.CONF)
    cfg.CONF(args=sys.argv[1:],
             project='a10_nlbaas2oct',
             version='a10_nlbaas2oct 1.0')
    logging.set_defaults()
    logging.setup(cfg.CONF, 'a10_nlbaas2oct')
    LOG = logging.getLogger('a10_nlbaas2oct')
    CONF.log_opt_values(LOG, logging.DEBUG)

    if not CONF.all and not CONF.lb_id and not CONF.project_id:
        print('Error: One of --all, --lb_id, --project_id must be specified.')
        return 1

    if ((CONF.all and (CONF.lb_id or CONF.project_id)) or
            (CONF.lb_id and CONF.project_id)):
        print('Error: Only one of --all, --lb_id, --project_id allowed.')
        return 1

    # TODO: IF a10 dbs not specified then default to lbaas dbs

    neutron_context_manager = enginefacade.transaction_context()
    neutron_context_manager.configure(
        connection=CONF.migration.neutron_db_connection)
    n_session_maker = neutron_context_manager.writer.get_sessionmaker()

    octavia_context_manager = enginefacade.transaction_context()
    octavia_context_manager.configure(
        connection=CONF.migration.octavia_db_connection)
    o_session_maker = octavia_context_manager.writer.get_sessionmaker()

    LOG.info('Starting migration.')

    nlbaas_session = n_session_maker(autocommit=False)

    lb_id_list = []
    if CONF.lb_id:
        lb_id_list = [[CONF.lb_id]]
    elif CONF.project_id:
        lb_id_list = nlbaas_session.execute(
            "SELECT id FROM neutron.lbaas_loadbalancers WHERE "
            "project_id = :id AND provisioning_status = 'ACTIVE';",
            {'id': CONF.project_id}).fetchall()
    else:  # CONF.ALL
        lb_id_list = nlbaas_session.execute(
            "SELECT id FROM neutron.lbaas_loadbalancers WHERE "
            "provisioning_status = 'ACTIVE';").fetchall()
    
    # TODO: Get tenant_id from the loadbalancer as well
    # Tenant_id is consumed by the thunder migration

    a10_config = a10_cfg.A10Config(config_dir=CONF.migration.a10_config_path,
                                   provider="a10networks")

    n_session = n_session_maker(autocommit=False)
    o_session = o_session_maker(autocommit=False)

    # Migrate the loadbalancers and their child objects
    failure_count = 0
    for lb_id in lb_id_list:
        try:
            lb_id = lb_id[0]
            # TODO: Preform a lookup of the associated device and cache it's name 
            # and associated tenant_id
            LOG.info('Locking load balancer: %s', lb_id)
            lock_loadbalancer(lb_id)
            #device_info = a10_config.get_device(device_name)
            #migrate_thunder(LOG, n_session_maker, o_session_maker, lb_id[0],
                            #tenant_id, device_info)

            n_lb = get_loadbalancery_entry(n_session, lb_id)
            if n_lb[0] != 'a10networks':
                LOG.info('Skipping loadbalancer with provider %s. Not an A10 Networks LB', n_lb[0])
                continue

            LOG.info('Migrating VIP port for load balancer: %s', lb_id)
            migrate_vip_ports(n_session, o_session, lb_id, n_lb,
                              CONF.migration.octavia_account_id)
            
            LOG.info('Migrating load balancer: %s', lb_id)
            migrate_lb(o_session, lb_id, n_lb)

            LOG.info('Migrating VIP for load balancer: %s', lb_id)
            migrate_vip(n_session, o_session, lb_id, n_lb)

            # Start listener migration
            listeners, lb_stats = get_listeners_and_stats_by_lb(n_session, lb_id)
            for listener in listeners:
                LOG.debug('Migrating listener: %s', listener[0])
                if listener[8] == 'DELETED':
                    continue
                elif listener[8] != 'ACTIVE':
                    raise Exception(_('Listener is invalid state of %s.'),
                                     listener[8])
                migrate_listener(o_session, lb_id, n_lb, listener, lb_stats)

                # Handle SNI certs
                SNIs = get_SNIs_by_listener(listener_id)
                for SNI in SNIs:
                    LOG.debug('Migrating SNI: %s', SNI[0])
                    migrate_SNI(n_session, o_session, listener[0], SNI)

                # Handle L7 policy records
                l7policies = get_l7policies_by_listner(listener[0])
                for l7policy in l7policies:
                    LOG.debug('Migrating L7 policy: %s', l7policy[0])
                    if l7policy[8] == 'DELETED':
                        continue
                    elif l7policy[8] != 'ACTIVE':
                        raise Exception(_('L7 policy is invalid state of %s.'),
                                        l7policy[8])                    
                    migrate_l7policy(LOG, n_session, o_session,
                                       listener[0], l7_policy, n_lb[1])
                    l7rules = get_l7rules_by_l7policy(l7policy[0])
                    for l7rule in l7rules:
                        LOG.debug('Migrating L7 rule: %s', l7policy[0])
                        if l7rule[6] == 'DELETED':
                            continue
                        elif l7rule[6] != 'ACTIVE':
                            raise Exception(_('L7 rule is invalid state of %s.'),
                                            l7rule[6])
                        migrate_l7rule(o_session, l7_policy, l7rule, n_lb[1])              

            # Start pool migration
            pools = get_pool_entries_by_lb()
            for pool in pools:
                migrate_pool()
                if pool[5] is not None:
                    migrate_health_monitor(LOG, n_session, o_session,
                                           n_lb[1], pool[0], pool[5])
                    # Handle the session persistence records
                    migrate_session_persistence(n_session, o_session, pool[0])
                    # Handle the pool memebers
                    migrate_members(LOG, n_session, o_session, n_lb[1], pool[0])


            # Delete the old neutron-lbaas records
            if (CONF.migration.delete_after_migration and not
                    CONF.migration.trial_run):
                cascade_delete_neutron_lb(n_session, lb_id)
            if CONF.migration.trial_run:
                o_session.rollback()
                n_session.rollback()
                LOG.info('Simulated migration of load balancer %s successful.',
                         lb_id)
            else:
                o_session.commit()
                n_session.commit()
                LOG.info('Migration of load balancer %s successful.', lb_id)
        except Exception as e:
            n_session.rollback()
            o_session.rollback()
            LOG.exception("Skipping load balancer %s due to: %s.", lb_id, str(e))
            failure_count += 1


    if failure_count:
        LOG.warning("%d failures were detected", failure_count)
        sys.exit(1)

if __name__ == "__main__":
    main()