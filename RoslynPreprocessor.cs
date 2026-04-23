using System;
using System.IO;
using System.Linq;
using System.Text;
using System.Collections.Generic;
using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.CSharp;
using Microsoft.CodeAnalysis.CSharp.Syntax;

namespace RoslynPreprocessor
{
    class Program
    {
        const string Sentinel = "===END_OF_CODE===";

        private static string GetIdentity(SyntaxNode node)
        {
            if (node is MethodDeclarationSyntax m) 
                return $"method:{m.Identifier.Text}({string.Join(",", m.ParameterList.Parameters.Select(p => p.Type?.ToString()))})";
            if (node is PropertyDeclarationSyntax p) 
                return $"prop:{p.Identifier.Text}";
            if (node is ConstructorDeclarationSyntax c) 
                return $"ctor:({string.Join(",", c.ParameterList.Parameters.Select(p => p.Type?.ToString()))})";
            return node.ToString();
        }

        private static List<SyntaxNode> GetSemanticNodes(string code, List<int> lines)
        {
            if (string.IsNullOrWhiteSpace(code)) return new List<SyntaxNode>();
            var tree = CSharpSyntaxTree.ParseText(code);
            var root = tree.GetRoot();
            var semanticNodes = new HashSet<SyntaxNode>();

            foreach (var lineNum in lines)
            {
                var text = tree.GetText();
                if (lineNum <= 0 || lineNum > text.Lines.Count) continue;
                var line = text.Lines[lineNum - 1];
                var node = root.FindNode(line.Span);
                while (node != null && 
                       !(node is MethodDeclarationSyntax) && 
                       !(node is PropertyDeclarationSyntax) && 
                       !(node is ConstructorDeclarationSyntax) && 
                       !(node is ClassDeclarationSyntax))
                {
                    node = node.Parent;
                }
                
                if (node != null)
                {
                    if (node is ClassDeclarationSyntax classNode)
                    {
                        foreach (var member in classNode.Members)
                        {
                            if (member is MethodDeclarationSyntax || member is PropertyDeclarationSyntax || member is ConstructorDeclarationSyntax)
                            {
                                if (member.Span.IntersectsWith(line.Span))
                                {
                                    semanticNodes.Add(member);
                                }
                            }
                        }
                    }
                    else
                    {
                        semanticNodes.Add(node);
                    }
                }
            }
            return semanticNodes.OrderBy(n => n.SpanStart).ToList();
        }

        private static object CreateChunk(SyntaxNode n)
        {
            if (n == null) return new { raw_code = "", clean_code = "" };

            var rawCode = n.NormalizeWhitespace().ToFullString();
            var commentKinds = new HashSet<SyntaxKind>
            {
                SyntaxKind.SingleLineCommentTrivia,
                SyntaxKind.MultiLineCommentTrivia,
                SyntaxKind.SingleLineDocumentationCommentTrivia,
                SyntaxKind.MultiLineDocumentationCommentTrivia,
                SyntaxKind.DocumentationCommentExteriorTrivia,
            };

            var cleanNode = n.ReplaceTrivia(
                n.DescendantTrivia(descendIntoTrivia: true)
                    .Concat(n.GetLeadingTrivia())
                    .Concat(n.GetTrailingTrivia())
                    .Where(t => commentKinds.Contains(t.Kind())),
                (original, _) => SyntaxFactory.ElasticMarker
            );
            var cleanCode = cleanNode.NormalizeWhitespace().ToFullString();

            return new { raw_code = rawCode, clean_code = cleanCode };
        }

        static void Main(string[] args)
        {
            Console.InputEncoding = Encoding.UTF8;
            Console.OutputEncoding = Encoding.UTF8;

            Console.WriteLine("READY");

            while (true)
            {
                string commandLine = Console.ReadLine();
                if (commandLine == null || commandLine == "EXIT") break;

                try
                {
                    if (commandLine.StartsWith("CLEAN|||"))
                    {
                        var codeBuilder = new StringBuilder();
                        while (true)
                        {
                            var line = Console.ReadLine();
                            if (line == null || line == Sentinel) break;
                            codeBuilder.AppendLine(line);
                        }
                        var code = codeBuilder.ToString();
                        var tree = CSharpSyntaxTree.ParseText(code);
                        var root = tree.GetRoot();
                        var cleanRoot = root.ReplaceTrivia(
                            root.DescendantTrivia(descendIntoTrivia: true)
                                .Where(t => t.IsKind(SyntaxKind.SingleLineCommentTrivia) || 
                                            t.IsKind(SyntaxKind.MultiLineCommentTrivia) || 
                                            t.IsKind(SyntaxKind.SingleLineDocumentationCommentTrivia) ||
                                            t.IsKind(SyntaxKind.MultiLineDocumentationCommentTrivia) ||
                                            t.IsKind(SyntaxKind.DocumentationCommentExteriorTrivia)),
                            (original, _) => SyntaxFactory.ElasticMarker
                        );
                        Console.WriteLine(cleanRoot.ToFullString());
                    }
                    else if (commandLine.StartsWith("DIFF_EXTRACT|||"))
                    {
                        var parts = commandLine.Split(new[] { "|||" }, StringSplitOptions.None);
                        var oldLns = parts[1].Split(new[] { ',' }, StringSplitOptions.RemoveEmptyEntries).Select(int.Parse).ToList();
                        var newLns = parts[2].Split(new[] { ',' }, StringSplitOptions.RemoveEmptyEntries).Select(int.Parse).ToList();

                        var codeBuilder = new StringBuilder();
                        while (true)
                        {
                            var line = Console.ReadLine();
                            if (line == null || line == Sentinel) break;
                            codeBuilder.AppendLine(line);
                        }

                        var combined = codeBuilder.ToString();
                        var codeParts = combined.Split(new[] { "---DELIMITER---" }, StringSplitOptions.None);
                        var oldCode = codeParts[0].Trim();
                        var newCode = codeParts.Length > 1 ? codeParts[1].Trim() : "";

                        var oldNodes = GetSemanticNodes(oldCode, oldLns);
                        var newNodes = GetSemanticNodes(newCode, newLns);

                        var oldTree = string.IsNullOrWhiteSpace(oldCode) ? null : CSharpSyntaxTree.ParseText(oldCode).GetRoot();
                        var newTree = string.IsNullOrWhiteSpace(newCode) ? null : CSharpSyntaxTree.ParseText(newCode).GetRoot();

                        var pairs = new List<object>();
                        var processedNewIdentities = new HashSet<string>();

                        foreach (var oldNode in oldNodes)
                        {
                            var identity = GetIdentity(oldNode);
                            SyntaxNode newNode = null;
                            if (newTree != null)
                            {
                                newNode = newTree.DescendantNodes()
                                    .FirstOrDefault(n => (n is MethodDeclarationSyntax || n is PropertyDeclarationSyntax || n is ConstructorDeclarationSyntax) && GetIdentity(n) == identity);
                            }

                            if (newNode != null) processedNewIdentities.Add(identity);

                            dynamic oldChunk = CreateChunk(oldNode);
                            dynamic newChunk = CreateChunk(newNode);

                            pairs.Add(new {
                                raw_old_code = oldChunk.raw_code,
                                clean_old_code = oldChunk.clean_code,
                                raw_new_code = newChunk.raw_code,
                                clean_new_code = newChunk.clean_code
                            });
                        }

                        foreach (var newNode in newNodes)
                        {
                            var identity = GetIdentity(newNode);
                            if (processedNewIdentities.Contains(identity)) continue;

                            SyntaxNode oldNode = null;
                            if (oldTree != null)
                            {
                                oldNode = oldTree.DescendantNodes()
                                    .FirstOrDefault(n => (n is MethodDeclarationSyntax || n is PropertyDeclarationSyntax || n is ConstructorDeclarationSyntax) && GetIdentity(n) == identity);
                            }

                            dynamic oldChunk = CreateChunk(oldNode);
                            dynamic newChunk = CreateChunk(newNode);

                            pairs.Add(new {
                                raw_old_code = oldChunk.raw_code,
                                clean_old_code = oldChunk.clean_code,
                                raw_new_code = newChunk.raw_code,
                                clean_new_code = newChunk.clean_code
                            });
                        }

                        Console.WriteLine(System.Text.Json.JsonSerializer.Serialize(pairs));
                    }
                }
                catch (Exception ex)
                {
                    Console.Error.WriteLine($"Error: {ex.Message}\n{ex.StackTrace}");
                }
                finally
                {
                    Console.WriteLine(Sentinel);
                }
            }
        }
    }
}
