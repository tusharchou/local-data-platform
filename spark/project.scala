// scala-cli project for the local-data-platform Spark job.
//
// Versions (checked against Maven Central on 2026-09-30):
//   * Spark 4.1.3 is the newest Spark release with a matching Iceberg runtime
//     (Spark 4.2.0 is out, but there is no iceberg-spark-runtime-4.2 yet).
//   * iceberg-spark-runtime-4.1_2.13 1.12.0 is the newest Iceberg runtime for Spark 4.1.
//   * Spark 4.1.3 is built with Scala 2.13.17 and needs Java 17 or later.
//   * sqlite-jdbc lets Iceberg's JdbcCatalog open the SQLite file pyiceberg's SqlCatalog writes.
//
// Keep these in step with SPARK_VERSION / ICEBERG_VERSION / SQLITE_JDBC_VERSION in
// src/local_data_platform/engine/spark/__init__.py.

//> using scala 2.13.17
//> using jvm temurin:17
//> using mainClass ldp.spark.IcebergJob
//> using resourceDir resources

//> using dep org.apache.spark::spark-sql:4.1.3
//> using dep org.apache.iceberg:iceberg-spark-runtime-4.1_2.13:1.12.0
//> using dep org.xerial:sqlite-jdbc:3.53.4.0

//> using options -deprecation -feature -unchecked -Xlint:_,-byname-implicit

// Spark reaches into JDK internals; on Java 17 these packages must be opened explicitly.
// This is the list Spark's own launcher (org.apache.spark.launcher.JavaModuleOptions) adds,
// which a plain `scala-cli run` does not go through.
//> using javaOpt -XX:+IgnoreUnrecognizedVMOptions
//> using javaOpt --add-modules=jdk.incubator.vector
//> using javaOpt --add-opens=java.base/java.lang=ALL-UNNAMED
//> using javaOpt --add-opens=java.base/java.lang.invoke=ALL-UNNAMED
//> using javaOpt --add-opens=java.base/java.lang.reflect=ALL-UNNAMED
//> using javaOpt --add-opens=java.base/java.io=ALL-UNNAMED
//> using javaOpt --add-opens=java.base/java.net=ALL-UNNAMED
//> using javaOpt --add-opens=java.base/java.nio=ALL-UNNAMED
//> using javaOpt --add-opens=java.base/java.util=ALL-UNNAMED
//> using javaOpt --add-opens=java.base/java.util.concurrent=ALL-UNNAMED
//> using javaOpt --add-opens=java.base/java.util.concurrent.atomic=ALL-UNNAMED
//> using javaOpt --add-opens=java.base/jdk.internal.ref=ALL-UNNAMED
//> using javaOpt --add-opens=java.base/sun.nio.ch=ALL-UNNAMED
//> using javaOpt --add-opens=java.base/sun.nio.cs=ALL-UNNAMED
//> using javaOpt --add-opens=java.base/sun.security.action=ALL-UNNAMED
//> using javaOpt --add-opens=java.base/sun.util.calendar=ALL-UNNAMED
//> using javaOpt -Djdk.reflect.useDirectMethodHandle=false
//> using javaOpt -Dio.netty.tryReflectionSetAccessible=true
//> using javaOpt -Xmx2g
