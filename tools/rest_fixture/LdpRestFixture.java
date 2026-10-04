// scala-cli project: the Apache Iceberg REST catalog test server, for local-data-platform tests and demos.
//
// It runs org.apache.iceberg.rest.RESTCatalogServer, the server Apache Iceberg publishes in the
// test-fixtures jar of org.apache.iceberg:iceberg-open-api (the same code as the
// apache/iceberg-rest-fixture Docker image), with a JdbcCatalog on a SQLite file as its backend and a
// local file:// warehouse. Nothing needs Docker; scala-cli fetches a JDK and the jars on first use.
//
//   scala-cli run tools/rest_fixture -- --port 8181 --warehouse /tmp/ldp-rest/warehouse
//
// The iceberg-open-api POM declares no dependencies (its test-fixtures dependencies live only in
// Iceberg's Gradle build), so the runtime classpath is listed here by hand. It mirrors the
// `testFixturesImplementation` block of `project(':iceberg-open-api')` in Iceberg's build.gradle at
// tag apache-iceberg-1.12.0, minus the aws/azure/gcp *bundles* (a file:// warehouse needs no cloud SDK):
//   * iceberg-core's `tests` jar: RESTCatalogAdapter and RESTCatalogServlet. It is referenced by URL
//     under a made-up module name, because coursier keeps only one classifier of iceberg-core.
//   * iceberg-aws / -gcp / -azure: RESTServerCatalogAdapter references their property classes.
//   * hadoop-common: RESTCatalogServer builds the catalog with a Hadoop Configuration, and the
//     JdbcCatalog's default FileIO (HadoopFileIO) writes the file:// warehouse. Its Jetty 9 and
//     logging dependencies are excluded, as Iceberg's build does, so they don't clash with Jetty 12.
//   * Jetty 12.1 (ee10 servlet + gzip compression), sqlite-jdbc and slf4j-simple.
// Versions come from gradle/libs.versions.toml at the same tag. Keep ICEBERG_VERSION in step with
// src/local_data_platform/engine/spark/__init__.py.

//> using jvm temurin:17
//> using mainClass org.apache.iceberg.rest.LdpRestFixture

//> using dep org.apache.iceberg:iceberg-open-api:1.12.0,classifier=test-fixtures
//> using dep org.apache.iceberg:iceberg-core:1.12.0
//> using dep org.apache.iceberg:iceberg-core-tests:1.12.0,url=https://repo1.maven.org/maven2/org/apache/iceberg/iceberg-core/1.12.0/iceberg-core-1.12.0-tests.jar
//> using dep org.apache.iceberg:iceberg-aws:1.12.0
//> using dep org.apache.iceberg:iceberg-gcp:1.12.0
//> using dep org.apache.iceberg:iceberg-azure:1.12.0
//> using dep org.apache.hadoop:hadoop-common:3.4.3,exclude=org.eclipse.jetty%jetty-server,exclude=org.eclipse.jetty%jetty-servlet,exclude=org.eclipse.jetty%jetty-webapp,exclude=org.eclipse.jetty%jetty-util,exclude=org.eclipse.jetty%jetty-util-ajax,exclude=ch.qos.reload4j%reload4j,exclude=org.slf4j%slf4j-reload4j,exclude=com.github.pjfanning%jersey-json,exclude=com.sun.jersey%jersey-core,exclude=com.sun.jersey%jersey-servlet,exclude=com.sun.jersey%jersey-server,exclude=org.apache.zookeeper%zookeeper,exclude=org.apache.curator%curator-client,exclude=org.apache.curator%curator-recipes,exclude=org.apache.kerby%kerb-core
//> using dep org.eclipse.jetty.ee10:jetty-ee10-servlet:12.1.13
//> using dep org.eclipse.jetty.compression:jetty-compression-server:12.1.13
//> using dep org.eclipse.jetty.compression:jetty-compression-gzip:12.1.13
//> using dep org.xerial:sqlite-jdbc:3.53.4.0
//> using dep org.slf4j:slf4j-simple:2.0.18

//> using javaOpt -Xmx1g
//> using javaOpt -Dorg.slf4j.simpleLogger.defaultLogLevel=warn

// Same package as RESTCatalogServer, whose Map-taking constructor is package-private.
package org.apache.iceberg.rest;

import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.HashMap;
import java.util.Map;

/**
 * Starts {@link RESTCatalogServer} on a chosen port with a persistent SQLite JdbcCatalog and a local
 * file warehouse, prints one {@code LDP_REST_FIXTURE_READY <uri>} line once it serves requests, and
 * runs until it is killed.
 *
 * <p>Arguments: {@code --port N} (default 8181), {@code --warehouse DIR} (required; created if
 * missing), {@code --catalog-db FILE} (default {@code DIR/rest_catalog.db}) and {@code --name NAME}
 * (the backend catalog name, default {@code rest_backend}). {@code CATALOG_*} environment variables
 * still configure anything else, as in the upstream fixture.
 */
public final class LdpRestFixture {
  private LdpRestFixture() {}

  public static void main(String[] args) throws Exception {
    Map<String, String> options = parse(args);
    String warehouseArg = options.get("warehouse");
    if (warehouseArg == null) {
      System.err.println("usage: LdpRestFixture --warehouse DIR [--port N] [--catalog-db FILE] [--name NAME]");
      System.exit(2);
    }
    Path warehouse = Paths.get(warehouseArg).toAbsolutePath().normalize();
    Files.createDirectories(warehouse);
    Path catalogDb =
        Paths.get(options.getOrDefault("catalog-db", warehouse.resolve("rest_catalog.db").toString()))
            .toAbsolutePath()
            .normalize();
    Files.createDirectories(catalogDb.getParent());
    String port = options.getOrDefault("port", "8181");

    Map<String, String> config = new HashMap<>();
    config.put(RESTCatalogServer.REST_PORT, port);
    config.put(RESTCatalogServer.CATALOG_NAME, options.getOrDefault("name", "rest_backend"));
    config.put("warehouse", warehouse.toUri().toString());
    config.put("uri", "jdbc:sqlite:" + catalogDb);
    config.put("jdbc.schema-version", "V1");

    RESTCatalogServer server = new RESTCatalogServer(config);
    Runtime.getRuntime()
        .addShutdownHook(
            new Thread(
                () -> {
                  try {
                    server.stop();
                  } catch (Exception e) {
                    // the JVM is exiting anyway
                  }
                }));
    server.start(false);
    System.out.println("LDP_REST_FIXTURE_READY http://127.0.0.1:" + port);
    System.out.flush();
    Thread.currentThread().join();
  }

  private static Map<String, String> parse(String[] args) {
    Map<String, String> options = new HashMap<>();
    for (int i = 0; i < args.length; i++) {
      String arg = args[i];
      if (!arg.startsWith("--") || i + 1 >= args.length) {
        throw new IllegalArgumentException("expected --key value pairs, got " + arg);
      }
      options.put(arg.substring(2), args[++i]);
    }
    return options;
  }
}
